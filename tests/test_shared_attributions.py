"""Workspace assessment imports are reviewed, atomic, and never tracing controls."""

import concurrent.futures
import copy
import csv
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from liquid_tracer import address_import
from liquid_tracer.common import TraceError
from liquid_tracer.shared_attributions import (FILENAME, LOCKNAME, apply_import, catalog, export_library,
                                               load_library, preview_import)

A, B = "SYNTHETIC-shared-attribution-A", "SYNTHETIC-shared-attribution-B"


class SharedAttributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "workspace"

    def apply(self, text, **options):
        plan = preview_import(self.root, text, **options)
        self.assertTrue(plan["valid"], plan["errors"])
        return apply_import(self.root, text, approval_sha256=plan["approval_sha256"], **options)

    def test_empty_library_and_preview_are_stable_and_side_effect_free(self):
        first = load_library(self.root)
        self.assertEqual(first, load_library(self.root))
        self.assertEqual(first["revision"], 0)
        self.assertEqual(first["rules"], {})
        self.assertEqual(catalog(self.root)["rows"], [])
        plan = preview_import(self.root, A)
        self.assertEqual(plan["library_id"], first["library_id"])
        self.assertEqual(plan["counts"]["add"], 1)
        self.assertEqual(plan["active_stops_to_save"], 0)
        self.assertFalse(self.root.exists())
        self.assertNotEqual(first["library_id"], load_library(self.base / "other")["library_id"])

    def test_csv_preserves_evidence_fields_but_strips_all_tracing_controls(self):
        text = ("\ufeffAddress,Name,confidence,source,notes,observed_at,enabled,stop_tracing,hop_limit\r\n"
                + A + ',"Service, X",confirmed,Client records,"First line\nSecond line",2026-10-03,true,true,2\r\n'
                + B + ',Unused service,suspected,Research,,2026-10-02,false,maybe,nonsense\r\n')
        with patch("liquid_tracer.api.http", side_effect=AssertionError("No network access")):
            plan = preview_import(self.root, text)
            self.assertEqual(plan["ignored_controls"], {"stop_tracing": 2, "hop_limit": 2})
            self.assertEqual(plan["ignored_control_rows"], 2)
            self.assertEqual(self.apply(text)["changed"], 2)
        library = load_library(self.root)
        self.assertEqual(set(library["rules"]), {A, B})
        rule = library["rules"][A]
        self.assertEqual((rule["name"], rule["notes"], rule["source"], rule["confidence"], rule["observed_at"]),
                         ("Service, X", "First line\nSecond line", "Client records", "confirmed", "2026-10-03"))
        self.assertFalse(library["rules"][B]["enabled"])
        for rule in library["rules"].values():
            self.assertIs(rule["stop_tracing"], False)
            self.assertIsNone(rule["hop_limit"])
        self.assertEqual({path.name for path in self.root.iterdir()}, {FILENAME, LOCKNAME})

    def test_duplicate_rows_ignore_nonshared_controls_and_reimports_are_idempotent(self):
        text = json.dumps([{"address": A, "name": "BTSE", "stop_tracing": True, "hop_limit": 1},
                           {"value": A, "entity": "btse", "stop": False, "hop_limit": 7}])
        result = self.apply(text)
        self.assertEqual((result["changed"], result["duplicate_rows"]), (1, 1))
        before = (self.root / FILENAME).read_bytes()
        result = self.apply(text)
        self.assertEqual(result["changed"], 0)
        self.assertEqual(result["counts"]["unchanged"], 1)
        self.assertEqual((self.root / FILENAME).read_bytes(), before)

    def test_keep_and_replace_preserve_creation_time_and_audit_previous_values(self):
        self.apply(json.dumps([{"address": A, "name": "Old", "notes": "Original evidence"}]))
        old = copy.deepcopy(load_library(self.root)["rules"][A])
        text = json.dumps([{"address": A, "name": "New", "enabled": False}, {"address": B, "name": "Other"}])
        kept = self.apply(text)
        self.assertEqual(kept["counts"], {"add": 1, "replace": 0, "keep": 1, "unchanged": 0})
        self.assertEqual(load_library(self.root)["rules"][A], old)
        replaced = self.apply(text, policy="replace")
        self.assertEqual(replaced["changed"], 1)
        library = load_library(self.root)
        self.assertEqual(library["rules"][A]["created_at"], old["created_at"])
        self.assertEqual(library["history"][-1]["previous"], old)
        self.assertEqual(library["history"][-1]["rule"], library["rules"][A])
        self.assertEqual(library["revision"], 3)
        self.assertFalse(library["rules"][A]["enabled"])

    def test_approval_binds_exact_source_options_workspace_and_library_revision(self):
        text = "Address,Name\n" + A + ",Service\n"
        plan = preview_import(self.root, text)
        for changed, options in ((text + "\n", {}), ("\ufeff" + text, {}), (text, {"policy": "replace"}),
                                 (text, {"format": "csv"})):
            with self.subTest(options=options), self.assertRaisesRegex(TraceError, "changed"):
                apply_import(self.root, changed, approval_sha256=plan["approval_sha256"], **options)
        with self.assertRaisesRegex(TraceError, "changed"):
            apply_import(self.base / "other", text, approval_sha256=plan["approval_sha256"])
        self.apply(B)
        with self.assertRaisesRegex(TraceError, "changed"):
            apply_import(self.root, text, approval_sha256=plan["approval_sha256"])
        self.assertEqual(set(load_library(self.root)["rules"]), {B})

    def test_concurrent_approved_writes_cannot_overwrite_each_other(self):
        plans = [(text, preview_import(self.root, text)) for text in (A, B)]
        barrier = threading.Barrier(2)

        def save(item):
            text, plan = item
            barrier.wait(timeout=3)
            try:
                return apply_import(self.root, text, approval_sha256=plan["approval_sha256"])
            except TraceError as error:
                return error

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(save, plans))
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual(sum(isinstance(item, TraceError) for item in results), 1)
        library = load_library(self.root)
        self.assertEqual((library["revision"], len(library["rules"]), len(library["history"])), (1, 1, 1))
        missing = B if A in library["rules"] else A
        self.apply(missing)
        self.assertEqual(set(load_library(self.root)["rules"]), {A, B})

    def test_invalid_attribution_rows_never_partially_apply(self):
        invalid = [{"address": B, "confidence": "corroborated"}, {"address": "https://invalid/address"},
                   {"address": B, "observed_at": "yesterday"}, {"address": B, "enabled": "maybe"},
                   {"address": B, "network": "bitcoin"}, {"address": B, "classification": "label"}]
        for row in invalid:
            text = json.dumps([A, row])
            with self.subTest(row=row):
                plan = preview_import(self.root, text)
                self.assertFalse(plan["valid"])
                self.assertIsNone(plan["approval_sha256"])
                with self.assertRaises(TraceError):
                    apply_import(self.root, text, approval_sha256="a" * 64)
                self.assertFalse((self.root / FILENAME).exists())
        for text in ("Address,Name\n" + A + ",One,Extra\n",
                     "Address,Name\n" + A + ",One\n" + A + ",Two\n"):
            self.assertFalse(preview_import(self.root, text)["valid"])

    def test_malformed_imports_and_size_row_limits_fail_without_files(self):
        invalid = [("", "auto"), ("[]", "json"), ("{}", "json"), ("[", "json"),
                   ('[{"address":"' + A + '","address":"' + B + '"}]', "json"),
                   ("Address,value\n" + A + "," + A, "csv"), ("Name\nService", "csv"),
                   ("x" * (address_import.MAX_BYTES + 1), "text"), (A, "unsupported"),
                   ("\n".join([A] * 5001), "text"), ("\ud800", "text")]
        for text, format in invalid:
            with self.subTest(format=format), self.assertRaises(TraceError):
                preview_import(self.root, text, format=format)
        self.assertFalse(self.root.exists())

    def test_catalog_search_pagination_and_export_round_trip(self):
        self.apply(json.dumps([{"address": A, "name": "Service X", "notes": "Unique evidence", "enabled": False},
                              {"address": B, "name": "Service Y", "confidence": "confirmed"}]))
        page = catalog(self.root, query="UNIQUE", offset=0, limit=1)
        self.assertEqual((page["total"], len(page["rows"]), page["rows"][0]["address"]), (1, 1, A))
        self.assertEqual(catalog(self.root, offset=1, limit=1)["rows"][0]["address"], B)
        product = export_library(self.root)
        rows = list(csv.DictReader(io.StringIO(product["data"].decode())))
        self.assertTrue(all(row["stop_tracing"] == "false" and row["hop_limit"] == "" for row in rows))
        self.assertEqual(rows[0]["enabled"], "false")
        self.assertEqual(self.apply(product["data"].decode())["changed"], 0)
        self.assertEqual(product["filename"], "shared-attributions.csv")
        self.assertEqual(product["data"], export_library(self.root)["data"])
        self.assertEqual(product["library_id"], load_library(self.root)["library_id"])

    def test_large_library_export_splits_into_reimportable_csv_parts(self):
        self.apply(A + "\n" + B)
        with patch("liquid_tracer.address_import.MAX_ROWS", 1):
            product = export_library(self.root)
            self.assertEqual(product["content_type"], "application/zip")
            with zipfile.ZipFile(io.BytesIO(product["data"])) as archive:
                self.assertEqual(len(archive.namelist()), 2)
                for name in archive.namelist():
                    self.assertTrue(preview_import(self.root, archive.read(name).decode())["valid"])

    def test_library_rejects_symlink_hardlink_directory_fifo_and_oversize_files(self):
        self.root.mkdir()
        target = self.base / "outside.json"
        target.write_text("Keep outside data")
        path = self.root / FILENAME
        for kind in ("symlink", "hardlink", "directory", "fifo", "oversize"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    path.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, path)
                elif kind == "directory":
                    path.mkdir()
                elif kind == "fifo":
                    os.mkfifo(path)
                else:
                    with path.open("wb") as stream:
                        stream.truncate(64 * 1024 * 1024 + 1)
                with self.assertRaises(TraceError):
                    load_library(self.root)
                path.rmdir() if kind == "directory" else path.unlink()
        self.assertEqual(target.read_text(), "Keep outside data")

    def test_lock_symlink_and_root_symlink_cannot_redirect_writes(self):
        self.root.mkdir()
        external = self.base / "external"
        external.mkdir()
        target = external / "lock"
        target.write_text("Keep external lock")
        plan = preview_import(self.root, A)
        (self.root / LOCKNAME).symlink_to(target)
        with self.assertRaises(TraceError):
            apply_import(self.root, A, approval_sha256=plan["approval_sha256"])
        self.assertEqual(target.read_text(), "Keep external lock")
        alias = self.base / "alias"
        alias.symlink_to(external, target_is_directory=True)
        with self.assertRaises(TraceError):
            preview_import(alias, A)
        self.assertFalse((external / FILENAME).exists())

    def test_invalid_saved_controls_metadata_or_history_fail_closed(self):
        self.apply(A)
        path = self.root / FILENAME
        original = path.read_bytes()
        data = json.loads(original)
        mutations = [lambda value: value.update(revision=True),
                     lambda value: value["rules"][A].update(stop_tracing=True),
                     lambda value: value["rules"][A].update(hop_limit=1),
                     lambda value: value["rules"][A].update(enabled="true"),
                     lambda value: value["history"][0].update(previous={}),
                     lambda value: value["history"][0].update(import_sha256="invalid")]
        for mutate in mutations:
            altered = copy.deepcopy(data)
            mutate(altered)
            path.write_text(json.dumps(altered))
            with self.assertRaises(TraceError):
                load_library(self.root)
        path.write_bytes(original)
        self.assertEqual(load_library(self.root)["revision"], 1)

    def test_failed_atomic_replace_preserves_library_and_cleans_temporary_file(self):
        self.apply(A)
        original = (self.root / FILENAME).read_bytes()
        plan = preview_import(self.root, B)
        with patch("liquid_tracer.shared_attributions.os.replace", side_effect=OSError("synthetic disk error")), \
                self.assertRaises(TraceError):
            apply_import(self.root, B, approval_sha256=plan["approval_sha256"])
        self.assertEqual((self.root / FILENAME).read_bytes(), original)
        self.assertEqual({path.name for path in self.root.iterdir()}, {FILENAME, LOCKNAME})

    def test_moved_library_retains_provenance_but_old_workspace_approval_does_not_transfer(self):
        self.apply(A)
        identity = load_library(self.root)["library_id"]
        plan = preview_import(self.root, B)
        moved = self.base / "moved"
        self.root.rename(moved)
        self.assertEqual(load_library(moved)["library_id"], identity)
        with self.assertRaisesRegex(TraceError, "changed"):
            apply_import(moved, B, approval_sha256=plan["approval_sha256"])
        new = preview_import(moved, B)
        self.assertEqual(apply_import(moved, B, approval_sha256=new["approval_sha256"])["changed"], 1)


if __name__ == "__main__":
    unittest.main()
