"""Spreadsheet helper columns never become imported investigation settings."""

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer import address_import, change_output_import, name_color_import
from liquid_tracer.common import TraceError
from liquid_tracer.investigations import create_investigation
from liquid_tracer.services import load_services, set_service


ADDRESS = "SYNTHETIC-helper-column-address"
TXID = "a" * 64
IMPORTS = (
    (address_import, ["Address", "Name", "confidence", "stop_tracing", "source", "notes"],
     [ADDRESS, "BTSE", "CONFIRMED", "false", "Research", "Reviewed"], "rules"),
    (change_output_import, ["Txid", "ChangeVout", "Notes"], [TXID, "1", "Reviewed"], "change_outputs"),
    (name_color_import, ["Name", "Color"], ["BTSE", "#AABBCC"], "name_colors"),
)


def source(headers, *rows):
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows([headers, *rows])
    return stream.getvalue()


class CsvExtraColumnsTests(unittest.TestCase):
    def test_address_auto_detection_preserves_large_plain_text_lists(self):
        text = " ".join("SYNTHETIC-long-address-" + str(index).zfill(8) for index in range(4500))
        parsed = address_import.parse_import(text)
        self.assertEqual(parsed["format"], "text")
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(len(parsed["rows"]), 4500)

    def test_shuffled_case_insensitive_columns_ignore_helper_values(self):
        for module, headers, values, _ in IMPORTS:
            with self.subTest(importer=module.__name__):
                expected = module.parse_import(source(headers, values), "csv")
                expanded_headers = ["Occurrences", *[" " + key.swapcase() + " " for key in headers[::-1]],
                                    "Calculation", "", "Count", "count", ""]
                expanded_values = ["2", *values[::-1], "#DIV/0!", "", "1", "99", "not imported"]
                actual = module.parse_import("\ufeff" + source(expanded_headers, expanded_values))
                self.assertEqual(actual["format"], "csv")
                for key in ("rows", "errors", "duplicates", "input_rows"):
                    self.assertEqual(actual[key], expected[key], key)

    def test_quoted_multiline_helpers_preserve_csv_mapping_and_line_numbers(self):
        for module, headers, values, _ in IMPORTS:
            with self.subTest(importer=module.__name__):
                text = source(["Duplicate\ncount", *headers, "Notes for, spreadsheet"],
                              ["1", *values, 'formula, "quoted"\nnext line'])
                parsed = module.parse_import(text)
                self.assertEqual(parsed["format"], "csv")
                self.assertEqual(parsed["errors"], [])
                self.assertEqual(parsed["rows"][0]["row"], 4)
                expected = module.parse_import(source(headers, values), "csv")["rows"][0]
                self.assertEqual(parsed["rows"][0], {**expected, "row": 4})

    def test_duplicate_rows_ignore_different_helper_counts(self):
        for module, headers, values, _ in IMPORTS:
            with self.subTest(importer=module.__name__):
                parsed = module.parse_import(source([*headers, "Count"],
                                                    [*values, "2"], [*values, "3"]))
                self.assertEqual(parsed["errors"], [])
                self.assertEqual((parsed["duplicates"], parsed["input_rows"], len(parsed["rows"])), (1, 2, 1))
                conflicting = values.copy()
                conflicting[-1] = "#112233" if module is name_color_import else "Changed note"
                rejected = module.parse_import(source([*headers, "Count"],
                                                       [*values, "2"], [*conflicting, "2"]))
                self.assertEqual(len(rejected["errors"]), 1)
                self.assertIn("Conflicting", rejected["errors"][0]["message"])

    def test_missing_required_and_duplicate_recognized_headers_are_rejected(self):
        for module, headers, values, _ in IMPORTS:
            with self.subTest(importer=module.__name__):
                with self.assertRaises(TraceError):
                    module.parse_import(source([headers[0] + " count", *headers[1:]], values), "csv")
                with self.assertRaisesRegex(TraceError, "unique"):
                    module.parse_import(source([*headers, headers[0].swapcase()], [*values, values[0]]), "csv")
        for extra in ("Value", "LABEL"):
            with self.subTest(alias=extra), self.assertRaisesRegex(TraceError, "unique"):
                address_import.parse_import(source(["Address", "Name", extra], [ADDRESS, "BTSE", "same"]), "csv")

    def test_unknown_columns_do_not_mask_incomplete_or_overlong_rows(self):
        for module, headers, values, _ in IMPORTS:
            for tail in (["1"], ["1", "2", "3"]):
                with self.subTest(importer=module.__name__, tail=tail):
                    parsed = module.parse_import(source([*headers, "Count", "Count"], [*values, *tail]))
                    self.assertEqual(parsed["rows"], [])
                    self.assertIn("different number of fields", parsed["errors"][0]["message"])

    def test_recognized_values_still_validate_and_helper_only_rows_are_invalid(self):
        cases = ((address_import, ["Address", "confidence"], [ADDRESS, "not a confidence"]),
                 (change_output_import, ["Txid", "ChangeVout"], [TXID, "not an index"]),
                 (name_color_import, ["Name", "Color"], ["BTSE", "not a color"]))
        for module, headers, values in cases:
            with self.subTest(importer=module.__name__):
                self.assertTrue(module.parse_import(source([*headers, "Count"], [*values, "1"]))["errors"])
                self.assertTrue(module.parse_import(source([*headers, "Count"], ["", "", "1"]))["errors"])

    def test_address_compatibility_guards_and_aliases_are_preserved(self):
        parsed = address_import.parse_import(source(["Count", "Value", "Entity", "Stop", "Kind", "Network"],
                                                    ["1", ADDRESS, "BTSE", "false", "address", "Liquid"]))
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["rows"][0]["rule"]["name"], "BTSE")
        self.assertFalse(parsed["rows"][0]["rule"]["stop_tracing"])
        for kind, network in (("script", "liquid"), ("address", "bitcoin")):
            rejected = address_import.parse_import(source(["Address", "Kind", "Network", "Count"],
                                                          [ADDRESS, kind, network, "1"]))
            self.assertTrue(rejected["errors"])

    def test_json_remains_strict_about_unsupported_fields(self):
        for module, headers, values, _ in IMPORTS:
            with self.subTest(importer=module.__name__):
                row = dict(zip(headers, values))
                row["Count"] = 1
                parsed = module.parse_import(json.dumps([row]))
                self.assertTrue(parsed["errors"])

    def test_blank_supported_cells_still_clear_only_with_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, module in enumerate((change_output_import, name_color_import)):
                with self.subTest(importer=module.__name__):
                    case = create_investigation(Path(directory), "Clear helper " + str(index))
                    headers, values, field = IMPORTS[index + 1][1:]
                    if module is name_color_import:
                        set_service(case, ADDRESS, name="BTSE")
                    initial = source(headers, values)
                    preview = module.preview_import(case, initial)
                    module.apply_import(case, initial, approval_sha256=preview["approval_sha256"])
                    empty = values.copy()
                    empty[1] = ""
                    text = source(["Count", *headers, "Helper"], ["1", *empty, "keep"])
                    self.assertEqual(module.preview_import(case, text)["counts"]["keep"], 1)
                    clear = module.preview_import(case, text, policy="replace")
                    self.assertEqual(clear["counts"]["clear"], 1)
                    module.apply_import(case, text, policy="replace", approval_sha256=clear["approval_sha256"])
                    self.assertFalse(load_services(case).get(field))

    def test_preview_apply_stores_only_recognized_fields_and_helper_edits_are_noops(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, (module, headers, values, field) in enumerate(IMPORTS):
                with self.subTest(importer=module.__name__):
                    case = create_investigation(Path(directory), "Spreadsheet " + str(index))
                    if module is name_color_import:
                        set_service(case, ADDRESS, name="BTSE")
                    text = source(["Occurrences", *headers], ["1", *values])
                    preview = module.preview_import(case, text)
                    self.assertTrue(preview["valid"], preview["errors"])
                    result = module.apply_import(case, text, approval_sha256=preview["approval_sha256"])
                    self.assertEqual(result["changed"], 1)
                    settings = load_services(case)
                    self.assertNotIn("Occurrences", json.dumps(settings))
                    self.assertNotIn("occurrences", json.dumps(settings))
                    self.assertTrue(settings[field])
                    before = (case / "services.json").read_bytes()
                    edited = source(["Occurrences", *headers], ["2", *values])
                    updated = module.preview_import(case, edited)
                    self.assertEqual(updated["counts"]["unchanged"], 1)
                    result = module.apply_import(case, edited, approval_sha256=updated["approval_sha256"])
                    self.assertEqual(result["changed"], 0)
                    self.assertEqual((case / "services.json").read_bytes(), before)

    def test_approval_still_binds_the_reviewed_file_including_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, (module, headers, values, _) in enumerate(IMPORTS):
                with self.subTest(importer=module.__name__):
                    case = create_investigation(Path(directory), "Reviewed " + str(index))
                    if module is name_color_import:
                        set_service(case, ADDRESS, name="BTSE")
                    text = source([*headers, "Count"], [*values, "1"])
                    preview = module.preview_import(case, text)
                    changed = source([*headers, "Count"], [*values, "2"])
                    with self.assertRaisesRegex(TraceError, "changed"):
                        module.apply_import(case, changed, approval_sha256=preview["approval_sha256"])
