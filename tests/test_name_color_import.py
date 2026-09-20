"""Bulk name colors are reviewed, atomic presentation updates without evidence edits."""

import copy
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer import address_import, name_color_import
from liquid_tracer.api import Esplora
from liquid_tracer.common import TraceError, digest, save_json
from liquid_tracer.export import build_graph
from liquid_tracer.investigations import create_investigation
from liquid_tracer.name_colors import set_name_colors
from liquid_tracer.role_colors import set_role_colors
from liquid_tracer.services import load_services, set_service
from tests.fixtures import A, B
from tests import test_service_presentation


class NameColorImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Imported colors", seeds=[A + ":0"])
        self.ingest([{"address": "SYNTHETIC-one", "name": "BTSE", "notes": "Keep this evidence"},
                     {"address": "SYNTHETIC-two", "name": "btse", "enabled": False},
                     {"address": "SYNTHETIC-three", "name": "Client wallet", "stop_tracing": False}])

    def ingest(self, rows):
        text = json.dumps(rows)
        plan = address_import.preview_import(self.case, text)
        return address_import.apply_import(self.case, text, approval_sha256=plan["approval_sha256"])

    def apply(self, text, **options):
        preview = name_color_import.preview_import(self.case, text, **options)
        self.assertTrue(preview["valid"], preview["errors"])
        return name_color_import.apply_import(self.case, text, approval_sha256=preview["approval_sha256"], **options)

    def test_csv_matches_case_insensitively_and_preserves_evidence(self):
        set_role_colors(self.case, [{"role": "seed", "color": "#654321"}],
                        expected_revision=load_services(self.case)["revision"])
        archive = self.case / "archived-evidence.json"
        archive.write_text('{"transaction":"unchanged"}\n')
        archived_bytes = archive.read_bytes()
        case_bytes = (self.case / "case.json").read_bytes()
        before = load_services(self.case)
        source = "nAmE,COLOR\nbTsE,#12ABCD\nClient wallet,#000000\n"
        plan = name_color_import.preview_import(self.case, source)
        self.assertEqual(plan["counts"], {"add": 2, "replace": 0, "clear": 0, "unchanged": 0, "keep": 0})
        self.assertEqual(plan["changes"][0], {"row": 2, "name": "bTsE", "key": "btse", "color": "#12abcd",
                                               "previous": None, "action": "add", "addresses": 2})
        with patch.object(Esplora, "get", side_effect=AssertionError("No chain requests")), \
                patch("liquid_tracer.api.http", side_effect=AssertionError("No network")), \
                patch("liquid_tracer.name_color_import.save_json", wraps=save_json) as save:
            result = self.apply(source)
        self.assertEqual(save.call_count, 1)
        after = load_services(self.case)
        self.assertEqual(result["changed"], 2)
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["name_colors"], {"btse": "#12abcd", "client wallet": "#000000"})
        for key in ("rules", "role_colors", "schema_version", "case_id"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["history"][:-1], before["history"])
        event = after["history"][-1]
        self.assertEqual(event["type"], "name_colors")
        self.assertEqual(event["import_id"], result["import_id"])
        self.assertEqual(event["import_sha256"], digest(source.encode()))
        self.assertEqual(event["previous"], {})
        self.assertEqual(event["name_colors"], after["name_colors"])
        self.assertEqual(archive.read_bytes(), archived_bytes)
        self.assertEqual((self.case / "case.json").read_bytes(), case_bytes)

    def test_keep_replace_clear_and_absent_names(self):
        self.apply("Name,Color\nBTSE,#123456\nClient wallet,#abcdef\n")
        source = "Name,Color\nbtse,\nClient wallet,#000000\n"
        before = (self.case / "services.json").read_bytes()
        result = self.apply(source)
        self.assertEqual(result["counts"]["keep"], 2)
        self.assertEqual(result["changed"], 0)
        self.assertIsNone(result["import_id"])
        self.assertEqual((self.case / "services.json").read_bytes(), before)
        result = self.apply(source, policy="replace")
        self.assertEqual(result["counts"]["clear"], 1)
        self.assertEqual(result["counts"]["replace"], 1)
        self.assertEqual(load_services(self.case)["name_colors"], {"client wallet": "#000000"})
        result = self.apply('[{"name":"BTSE","color":null}]', policy="replace")
        self.assertEqual(result["counts"]["unchanged"], 1)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(load_services(self.case)["name_colors"], {"client wallet": "#000000"})

    def test_identical_duplicates_coalesce_across_case_and_hex_case(self):
        source = '[{"Name":"BTSE","Color":"#ABCDEF"},{"NAME":"btse","COLOR":"#abcdef"}]'
        plan = name_color_import.preview_import(self.case, source)
        self.assertEqual((plan["input_rows"], plan["unique_names"], plan["duplicate_rows"]), (2, 1, 1))
        self.assertEqual(plan["format"], "json")
        result = self.apply(source)
        self.assertEqual(result["changed"], 1)
        before = (self.case / "services.json").read_bytes()
        self.assertEqual(self.apply(source)["changed"], 0)
        self.assertEqual((self.case / "services.json").read_bytes(), before)
        plan = name_color_import.preview_import(self.case,
            '[{"name":"BTSE","color":null},{"name":"btse","color":" "}]')
        self.assertTrue(plan["valid"])
        self.assertEqual(plan["duplicate_rows"], 1)

    def test_conflicts_unknown_names_and_malformed_rows_are_all_or_nothing(self):
        before = (self.case / "services.json").read_bytes()
        for source in (
            "Name,Color\nClient wallet,#000000\nBTSE,#abcdef\nbtse,#654321\n",
            "Name,Color\nBTSE,#abcdef\nNew exchange,#654321\n",
            "Name,Color\nBTSE,#abcdef\nClient wallet\n",
            "Name,Color\nBTSE,#abcdef,extra\n",
            '[{"name":"BTSE","color":"#abcdef"},{"name":"Client wallet"}]',
            '[{"name":"BTSE","color":"#abcdef","role":"seed"}]',
            '[{"name":"BTSE","color":"#abcdef","NAME":"btse"}]',
            '[{"name":"BTSE","color":"#abcdef"},null]',
            "Name,Color\nBTSE,#abcdef\nbtse,\n",
        ):
            with self.subTest(source=source):
                plan = name_color_import.preview_import(self.case, source)
                self.assertFalse(plan["valid"])
                self.assertIsNone(plan["approval_sha256"])
                self.assertTrue(plan["errors"])
                with self.assertRaises(TraceError):
                    name_color_import.apply_import(self.case, source, approval_sha256="0" * 64)
                self.assertEqual((self.case / "services.json").read_bytes(), before)
        unknown = name_color_import.preview_import(self.case, "Name,Color\nUnknown,#000000\n")
        self.assertIn("import or save", unknown["errors"][0]["message"])

    def test_rejects_missing_headers_duplicate_fields_and_malformed_sources(self):
        for source in ("", "Name,Color\n", "Name,role\nBTSE,seed\n",
                       "Name,Color,NAME\nBTSE,#123456,btse\n", "Name\nBTSE\n",
                       '{"Name":"BTSE","Color":"#123456"}', "[]",
                       '[{"name":"BTSE","name":"btse","color":"#123456"}]',
                       'Name,Color\n"BTSE,#123456', "[{]"):
            with self.subTest(source=source), self.assertRaises(TraceError):
                name_color_import.preview_import(self.case, source)
        for format in ("text", [], None):
            with self.subTest(format=format), self.assertRaises(TraceError):
                name_color_import.preview_import(self.case, "Name,Color\nBTSE,#123456\n", format=format)
        for policy in ("merge", [], None):
            with self.subTest(policy=policy), self.assertRaises(TraceError):
                name_color_import.preview_import(self.case, "Name,Color\nBTSE,#123456\n", policy=policy)

    def test_rejects_unsafe_colors_and_invalid_names(self):
        for color in ("red", "#fff", "#12345g", "#12345678", "#000000;background:red", True, [], 12):
            with self.subTest(color=color):
                plan = name_color_import.preview_import(self.case, json.dumps([{"name": "BTSE", "color": color}]))
                self.assertFalse(plan["valid"])
        for name in (None, "", "\t", [], "a\nb", "a" * 361):
            with self.subTest(name=name):
                plan = name_color_import.preview_import(self.case, json.dumps([{"name": name, "color": "#123456"}]))
                self.assertFalse(plan["valid"])

    def test_unicode_and_unused_name_assignment_are_supported(self):
        self.ingest([{"address": "SYNTHETIC-unicode", "name": "Straße"}])
        self.apply("Name,Color\nSTRASSE,#AABBCC\n")
        self.assertEqual(load_services(self.case)["name_colors"]["strasse"], "#aabbcc")
        set_service(self.case, "SYNTHETIC-unicode", name="Renamed")
        plan = name_color_import.preview_import(self.case, "Name,Color\nstrasse,\n", policy="replace")
        self.assertEqual(plan["changes"][0]["addresses"], 0)
        self.apply("Name,Color\nstrasse,\n", policy="replace")
        self.assertNotIn("strasse", load_services(self.case)["name_colors"])

    def test_approval_binds_source_options_case_and_full_settings(self):
        source = "Name,Color\nBTSE,#123456\n"
        plan = name_color_import.preview_import(self.case, source)
        for options in ({"text": source + "\n"}, {"text": "\ufeff" + source},
                        {"text": source, "format": "csv"}, {"text": source, "policy": "replace"}):
            with self.subTest(options=options), self.assertRaises(TraceError):
                name_color_import.apply_import(self.case, approval_sha256=plan["approval_sha256"], **options)
        other = create_investigation(self.root, "Other case", seeds=[A + ":0"])
        set_service(other, "SYNTHETIC-other", name="BTSE")
        with self.assertRaises(TraceError):
            name_color_import.apply_import(other, source, approval_sha256=plan["approval_sha256"])
        # Even an out-of-band settings edit without a revision bump invalidates review.
        settings = load_services(self.case)
        settings["history"].append({"note": "Edited after review"})
        save_json(self.case / "services.json", settings)
        with self.assertRaises(TraceError):
            name_color_import.apply_import(self.case, source, approval_sha256=plan["approval_sha256"])
        plan = name_color_import.preview_import(self.case, source)
        set_name_colors(self.case, [{"name": "Client wallet", "color": "#abcdef"}],
                        expected_revision=load_services(self.case)["revision"])
        with self.assertRaises(TraceError):
            name_color_import.apply_import(self.case, source, approval_sha256=plan["approval_sha256"])

    def test_stale_attribution_approval_after_color_import(self):
        source = "Address,Name\nSYNTHETIC-new,New exchange\n"
        plan = address_import.preview_import(self.case, source)
        self.apply("Name,Color\nBTSE,#123456\n")
        with self.assertRaises(TraceError):
            address_import.apply_import(self.case, source, approval_sha256=plan["approval_sha256"])

    def test_trace_locks_block_import_and_both_locks_protect_single_save(self):
        source = "Name,Color\nBTSE,#123456\n"
        plan = name_color_import.preview_import(self.case, source)
        for mode in (fcntl.LOCK_SH, fcntl.LOCK_EX):
            with (self.case / "trace.lock").open("a") as lock:
                fcntl.flock(lock, mode | fcntl.LOCK_NB)
                with self.assertRaisesRegex(TraceError, "active"):
                    name_color_import.apply_import(self.case, source, approval_sha256=plan["approval_sha256"])

        def guarded_save(path, settings):
            for filename in ("trace.lock", "case.lock"):
                with (self.case / filename).open("a") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            save_json(path, settings)

        with patch("liquid_tracer.name_color_import.save_json", side_effect=guarded_save) as save:
            self.apply(source)
        self.assertEqual(save.call_count, 1)

    def test_batch_larger_than_catalog_page_has_one_revision_and_event(self):
        self.ingest([{"address": "SYNTHETIC-" + str(i), "name": "Group " + str(i)} for i in range(150)])
        source = "Name,Color\n" + "".join("Group " + str(i) + ",#123456\n" for i in range(150))
        before = load_services(self.case)
        result = self.apply(source)
        after = load_services(self.case)
        self.assertEqual(result["changed"], 150)
        self.assertEqual(len(after["name_colors"]), 150)
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(len(after["history"]), len(before["history"]) + 1)
        self.assertEqual(after["rules"], before["rules"])

    def test_source_limits_and_regular_utf8_files(self):
        text = "\ufeffName,Color\nBTSE,#ABCDEF\n"
        path = self.root / "colors.csv"
        path.write_bytes(text.encode("utf-8"))
        self.assertEqual(name_color_import.read_import(path), text)
        self.assertTrue(name_color_import.preview_import(self.case, text)["valid"])
        parsed = name_color_import.parse_import("Name,Color\n" + "BTSE,#abcdef\n" * 5000)
        self.assertEqual((parsed["input_rows"], parsed["duplicates"]), (5000, 4999))
        for source in ("Name,Color\n" + "BTSE,#abcdef\n" * 5001,
                       "x" * (name_color_import.MAX_BYTES + 1), "\ud800"):
            with self.subTest(length=len(source)), self.assertRaises(TraceError):
                name_color_import.parse_import(source)
        path.write_bytes(b"x" * (name_color_import.MAX_BYTES + 1))
        with self.assertRaisesRegex(TraceError, "512 KiB"):
            name_color_import.read_import(path)
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(TraceError, "UTF-8"):
            name_color_import.read_import(path)
        fifo = self.root / "colors.pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(TraceError, "regular"):
            name_color_import.read_import(fifo)


class NameColorImportGraphTests(unittest.TestCase):
    setUp = test_service_presentation.ServicePresentationTests.setUp

    def test_imported_colors_flow_through_graph_without_changing_evidence_or_seed_priority(self):
        case = create_investigation(self.root / "investigations", "Graph colors", seeds=[A + ":0"])
        for address in ("SYNTHETIC-victim-deposit", "SYNTHETIC-branch-A"):
            set_service(case, address, name="Perpetrator", notes="Original analysis", stop_tracing=False)
        set_role_colors(case, [{"role": "seed", "color": "#880000"}],
                        expected_revision=load_services(case)["revision"])
        source = "Name,Color\nperpetrator,#123abc\n"
        preview = name_color_import.preview_import(case, source)
        name_color_import.apply_import(case, source, approval_sha256=preview["approval_sha256"])
        settings = load_services(case)
        self.state["labels"] = [{"kind": "address", "value": address, "entity": "Perpetrator",
                                "confidence": "suspected", "notes": "Original analysis"}
                               for address in ("SYNTHETIC-victim-deposit", "SYNTHETIC-branch-A")]
        self.state["service_controls"] = settings
        before = copy.deepcopy(self.state)
        with patch.object(Esplora, "get", side_effect=AssertionError("No chain requests")):
            graph = build_graph(self.state)
        self.assertEqual(self.state, before)
        seed = test_service_presentation.ServicePresentationTests.node(graph, A + ":0")
        child = test_service_presentation.ServicePresentationTests.node(graph, B + ":0")
        self.assertEqual((seed["role"], seed["color"]), ("seed", "#880000"))
        self.assertEqual((child["color_source"], child["color"]), ("name", "#123abc"))
        self.assertEqual(child["details"]["address_attributions"][0]["notes"], "Original analysis")


if __name__ == "__main__":
    unittest.main()
