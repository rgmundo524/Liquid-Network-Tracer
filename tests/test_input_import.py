"""Several CSV types share one review and one all-or-nothing settings write."""

import copy
import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer import input_import as imports
from liquid_tracer.common import TraceError, digest, save_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.services import load_services, set_service
from tests.fixtures import A, B
from tests.test_change_outputs import save_archive


class InputImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Combined inputs", seeds=[A + ":0"])
        self.attributions = {"name": "addresses.csv", "text":
            "Address,Name,stop_tracing,hop_limit,notes\n"
            "SYNTHETIC-one,Exchange,false,1,Investigator evidence\n"
            "SYNTHETIC-two,exchange,false,,Keep spelling\n"}
        self.colors = {"name": "colors.csv", "text": "Name,Color\nEXCHANGE,#12ABCD\n"}
        self.change = {"name": "change.csv", "text": f"Txid,ChangeVout,Notes\n{A},0,Reviewed output\n"}

    def apply(self, files):
        preview = imports.preview_import(self.case, files)
        self.assertTrue(preview["valid"], preview["errors"])
        return imports.apply_import(self.case, files, approval_sha256=preview["approval_sha256"])

    def test_all_types_reverse_order_single_save_and_existing_audit_schemas(self):
        files = [self.colors, self.change, self.attributions]
        before = sorted(path.name for path in self.case.iterdir())
        preview = imports.preview_import(self.case, files)
        self.assertEqual(sorted(path.name for path in self.case.iterdir()), before)
        self.assertTrue(preview["valid"], preview["errors"])
        self.assertEqual([row["kind"] for row in preview["files"]],
                         ["name-colors", "change-outputs", "attributions"])
        self.assertEqual(preview["files"][0]["changes"][0]["addresses"], 2)
        self.assertEqual(preview["counts"]["add"], 4)
        with patch("liquid_tracer.api.http", side_effect=AssertionError("No network")), \
                patch("liquid_tracer.input_import.save_json", wraps=save_json) as save:
            result = imports.apply_import(self.case, files, approval_sha256=preview["approval_sha256"])
        self.assertEqual(save.call_count, 1)
        self.assertEqual((result["changed"], result["revision"]), (4, 4))
        self.assertEqual([row["name"] for row in result["files"]], [row["name"] for row in files])
        settings = load_services(self.case)
        self.assertEqual(settings["name_colors"], {"exchange": "#12abcd"})
        self.assertEqual(settings["change_outputs"][A]["vout"], 0)
        self.assertEqual(settings["rules"]["SYNTHETIC-one"]["hop_limit"], 1)
        self.assertFalse(settings["rules"]["SYNTHETIC-one"]["stop_tracing"])
        self.assertEqual(settings["rules"]["SYNTHETIC-two"]["name"], "exchange")
        history = settings["history"]
        self.assertEqual([event["revision"] for event in history], [1, 2, 3, 4])
        self.assertEqual({event["import_id"] for event in history}, {result["import_id"]})
        self.assertEqual(history[0]["import_row"], 2)
        self.assertEqual(history[0]["import_sha256"], digest(self.attributions["text"].encode()))
        self.assertEqual(history[2]["type"], "name_colors")
        self.assertEqual(history[2]["import_sha256"], digest(self.colors["text"].encode()))
        self.assertEqual(history[2]["import_policy"], "keep")
        self.assertEqual(history[3]["type"], "change_outputs")
        self.assertEqual(history[3]["import_format"], "csv")

    def test_invalid_file_prevents_other_valid_files_from_being_saved(self):
        files = [self.attributions, {**self.colors, "text": "Name,Color\nExchange,invalid\n"}, self.change]
        preview = imports.preview_import(self.case, files)
        self.assertFalse(preview["valid"])
        self.assertIsNone(preview["approval_sha256"])
        self.assertEqual(preview["errors"][0]["file"], "colors.csv")
        self.assertEqual(preview["errors"][0]["file_index"], 1)
        self.assertEqual(preview["errors"][0]["row"], 2)
        with patch("liquid_tracer.input_import.save_json") as save:
            with self.assertRaisesRegex(TraceError, "preview and approve"):
                imports.apply_import(self.case, files, approval_sha256="0" * 64)
        save.assert_not_called()
        self.assertFalse((self.case / "services.json").exists())

    def test_known_invalid_change_output_prevents_attribution_write(self):
        archive = save_archive(self.case)
        evidence = (archive / "trace.json").read_bytes()
        files = [self.attributions, {**self.change, "text": f"Txid,ChangeVout\n{A},99\n"}]
        plan = imports.preview_import(self.case, files)
        self.assertFalse(plan["valid"])
        self.assertIn("does not exist", plan["errors"][0]["message"])
        with self.assertRaises(TraceError):
            imports.apply_import(self.case, files, approval_sha256="0" * 64)
        self.assertFalse((self.case / "services.json").exists())
        self.assertEqual((archive / "trace.json").read_bytes(), evidence)

    def test_keep_replace_clear_and_noop_have_existing_semantics(self):
        self.apply([self.attributions, self.colors, self.change])
        files = [{**self.attributions, "text": self.attributions["text"].replace("false,1", "false,3")},
                 {**self.colors, "text": "Name,Color\nExchange,#000000\n"},
                 {**self.change, "text": f"Txid,ChangeVout,Notes\n{A},1,Changed assessment\n"}]
        before = (self.case / "services.json").read_bytes()
        with patch("liquid_tracer.input_import.save_json") as save:
            result = self.apply(files)
        save.assert_not_called()
        self.assertEqual((result["changed"], result["counts"]["keep"]), (0, 3))
        self.assertIsNone(result["import_id"])
        self.assertEqual((self.case / "services.json").read_bytes(), before)
        result = self.apply([{**file, "policy": "replace"} for file in files])
        self.assertEqual((result["changed"], result["counts"]["replace"]), (3, 3))
        settings = load_services(self.case)
        self.assertEqual(settings["rules"]["SYNTHETIC-one"]["hop_limit"], 3)
        self.assertEqual(settings["name_colors"]["exchange"], "#000000")
        self.assertEqual(settings["change_outputs"][A]["vout"], 1)
        result = self.apply([
            {"name": "colors.csv", "text": "Name,Color\nExchange,\n", "policy": "replace"},
            {"name": "change.csv", "text": f"Txid,ChangeVout\n{A},\n", "policy": "replace"}])
        self.assertEqual(result["counts"]["clear"], 2)
        self.assertEqual(load_services(self.case)["name_colors"], {})
        self.assertEqual(load_services(self.case)["change_outputs"], {})

    def test_color_projection_respects_kept_attributions(self):
        set_service(self.case, "SYNTHETIC-one", name="Old name")
        files = [self.colors, self.attributions]
        # Remove the second, new address so keep cannot introduce Exchange.
        files[1] = {**self.attributions, "text": "Address,Name\nSYNTHETIC-one,Exchange\n"}
        preview = imports.preview_import(self.case, files)
        self.assertFalse(preview["valid"])
        self.assertIn("Unknown attribution name", preview["errors"][0]["message"])
        files[1]["policy"] = "replace"
        self.assertEqual(self.apply(files)["changed"], 2)
        self.assertEqual(load_services(self.case)["name_colors"], {"exchange": "#12abcd"})

    def test_detection_normalizes_bom_aliases_and_ignores_helper_columns(self):
        files = [
            {"name": "anything.dat", "text": "\ufeff Value , Service-Name , stop ,Spreadsheet,,Spreadsheet\n"
                "SYNTHETIC-one,Exchange,no,helper,empty,repeated\n"},
            {"name": "colors.csv", "text": " nAmE , COLOR ,helper\nexchange,#aBcDeF,ignored\n"},
            {"name": "change.csv", "text": f" TXID , ChangeVout ,helper\n{A.upper()},0,ignored\n"},
        ]
        plan = imports.preview_import(self.case, files)
        self.assertTrue(plan["valid"], plan["errors"])
        self.assertEqual([file["kind"] for file in plan["files"]], list(imports.KINDS))
        self.assertEqual(self.apply(files)["changed"], 3)

    def test_ambiguous_detection_requires_explicit_type(self):
        file = {"name": "combined.csv", "text": "Address,Name,Color\nSYNTHETIC-one,Exchange,#123456\n"}
        plan = imports.preview_import(self.case, [file])
        self.assertFalse(plan["valid"])
        self.assertEqual(plan["files"][0]["kind"], "auto")
        self.assertIn("choose its type", plan["errors"][0]["message"])
        result = self.apply([{**file, "kind": "attributions"}])
        self.assertEqual(result["changed"], 1)
        self.assertNotIn("name_colors", load_services(self.case))
        result = self.apply([{**file, "kind": "name-colors"}])
        self.assertEqual(result["changed"], 1)
        self.assertEqual(load_services(self.case)["name_colors"], {"exchange": "#123456"})

    def test_duplicate_kinds_are_reported_for_both_files(self):
        files = [self.attributions, {"name": "more.csv", "text": "Value,Label\nSYNTHETIC-three,Third\n"}]
        plan = imports.preview_import(self.case, files)
        self.assertFalse(plan["valid"])
        self.assertEqual([error["file"] for error in plan["errors"]], ["addresses.csv", "more.csv"])
        self.assertTrue(all("one CSV" in error["message"] for error in plan["errors"]))
        with self.assertRaises(TraceError):
            imports.apply_import(self.case, files, approval_sha256="0" * 64)

    def test_bad_metadata_content_and_headers_return_file_errors(self):
        for file in (None, {}, {"name": "", "text": "Address\nSYNTHETIC-one\n"},
                     {**self.attributions, "path": "/server/file.csv"},
                     {**self.attributions, "kind": []}, {**self.attributions, "policy": []},
                     {"name": "empty.csv", "text": ""}, {"name": "unknown.csv", "text": "Unknown\nvalue\n"},
                     {"name": "long.csv", "text": "x" * (imports.MAX_BYTES + 1)},
                     {"name": "unicode.csv", "text": "\ud800"}, {"name": "number.csv", "text": 42},
                     {"name": "header.csv", "text": "Address,value\nSYNTHETIC-one,SYNTHETIC-one\n"},
                     {"name": "quotes.csv", "text": '"Address\nSYNTHETIC-one\n'},
                     {"name": "rows.csv", "text": "Address,Name\nSYNTHETIC-one\n"}):
            with self.subTest(file=str(file)[:100]):
                plan = imports.preview_import(self.case, [file])
                self.assertFalse(plan["valid"])
                self.assertTrue(plan["files"][0]["errors"])
                self.assertEqual(plan["files"][0]["file_index"], 0)
                self.assertIsNone(plan["approval_sha256"])
        self.assertFalse((self.case / "services.json").exists())

    def test_file_and_row_limits(self):
        for files in (None, {}, [], [self.attributions] * 4):
            with self.subTest(files=files), self.assertRaisesRegex(TraceError, "one to three"):
                imports.preview_import(self.case, files)
        file = {"name": "rows.csv", "text": "Address\n" + "SYNTHETIC-one\n" * 5000}
        plan = imports.preview_import(self.case, [file])
        self.assertTrue(plan["valid"], plan["errors"])
        self.assertEqual(plan["files"][0]["duplicate_rows"], 4999)
        file["text"] += "SYNTHETIC-one\n"
        plan = imports.preview_import(self.case, [file])
        self.assertFalse(plan["valid"])
        self.assertIn("5,000", plan["errors"][0]["message"])

    def test_stale_hash_binds_sources_options_names_and_order(self):
        files = [self.attributions, self.colors, self.change]
        approval = imports.preview_import(self.case, files)["approval_sha256"]
        mutations = []
        for key, value in (("text", "\ufeff" + self.attributions["text"]),
                           ("text", self.attributions["text"] + "\n"),
                           ("name", "renamed.csv"), ("kind", "attributions"), ("policy", "replace")):
            changed = copy.deepcopy(files)
            changed[0][key] = value
            mutations.append(changed)
        mutations.append(list(reversed(files)))
        mutations.append(files[:2])
        for changed in mutations:
            with self.subTest(files=[file["name"] for file in changed]), self.assertRaisesRegex(TraceError, "changed"):
                imports.apply_import(self.case, changed, approval_sha256=approval)
        self.assertFalse((self.case / "services.json").exists())

    def test_stale_hash_binds_case_full_settings_and_saved_evidence(self):
        files = [self.attributions, self.change]
        approval = imports.preview_import(self.case, files)["approval_sha256"]
        other = create_investigation(self.root, "Other case")
        with self.assertRaisesRegex(TraceError, "changed"):
            imports.apply_import(other, files, approval_sha256=approval)
        save_archive(self.case)
        with self.assertRaisesRegex(TraceError, "changed"):
            imports.apply_import(self.case, files, approval_sha256=approval)
        approval = imports.preview_import(self.case, files)["approval_sha256"]
        set_service(self.case, "SYNTHETIC-old", name="Existing")
        with self.assertRaisesRegex(TraceError, "changed"):
            imports.apply_import(self.case, files, approval_sha256=approval)
        approval = imports.preview_import(self.case, files)["approval_sha256"]
        settings = load_services(self.case)
        settings["history"].append({"out_of_band": True})
        save_json(self.case / "services.json", settings)
        before = (self.case / "services.json").read_bytes()
        with self.assertRaisesRegex(TraceError, "changed"):
            imports.apply_import(self.case, files, approval_sha256=approval)
        self.assertEqual((self.case / "services.json").read_bytes(), before)

    def test_trace_lock_blocks_and_both_locks_cover_the_single_write(self):
        files = [self.colors, self.change, self.attributions]
        approval = imports.preview_import(self.case, files)["approval_sha256"]
        with (self.case / "trace.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TraceError, "active"):
                imports.apply_import(self.case, files, approval_sha256=approval)

        def guarded_save(path, data):
            for filename in ("trace.lock", "case.lock"):
                with (self.case / filename).open("a") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            save_json(path, data)

        with patch("liquid_tracer.input_import.save_json", side_effect=guarded_save) as save:
            self.apply(files)
        self.assertEqual(save.call_count, 1)

    def test_preserves_evidence_case_metadata_and_unrelated_settings(self):
        self.apply([self.attributions])
        settings = load_services(self.case)
        settings["role_colors"] = {"seed": "#123456"}
        save_json(self.case / "services.json", settings)
        archive = save_archive(self.case)
        before = {path: path.read_bytes() for path in (self.case / "case.json", archive / "trace.json",
                                                       archive / "graph.json", archive / "SHA256SUMS")}
        self.apply([self.colors, self.change])
        after = load_services(self.case)
        self.assertEqual(after["rules"], settings["rules"])
        self.assertEqual(after["role_colors"], settings["role_colors"])
        self.assertEqual(after["history"][:len(settings["history"])], settings["history"])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_invalid_approval_never_saves(self):
        for approval in (None, "", "0" * 63, "g" * 64, []):
            with self.subTest(approval=approval), self.assertRaises(TraceError):
                imports.apply_import(self.case, [self.attributions], approval_sha256=approval)
        self.assertFalse((self.case / "services.json").exists())

    def test_read_import_is_bounded_regular_utf8_and_preserves_bom(self):
        path = self.root / "input.csv"
        text = "\ufeff" + self.attributions["text"]
        path.write_bytes(text.encode())
        self.assertEqual(imports.read_import(path), text)
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(TraceError, "UTF-8"):
            imports.read_import(path)
        path.write_bytes(b"x" * (imports.MAX_BYTES + 1))
        with self.assertRaisesRegex(TraceError, "512 KiB"):
            imports.read_import(path)
        with self.assertRaisesRegex(TraceError, "regular CSV"):
            imports.read_import(self.root)


if __name__ == "__main__":
    unittest.main()
