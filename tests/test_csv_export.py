import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import TraceError, digest, save_json
from liquid_tracer.csv_export import export_csv
from liquid_tracer.export import COLORS, build_graph, export_run
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import A, B, fixture


DETAIL_FILES = ("inputs.csv", "outputs.csv", "spends.csv", "events.csv", "frontier.csv")


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


class CSVExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "saved-run"
        self.destination = self.root / "exports" / "csv-bundle"
        store = Store(self.root / "case")
        self.addCleanup(store.close)
        fixture_path = self.root / "fixture.json"
        data = fixture()
        # A provider-supplied address exercises archived spreadsheet protection.
        data["/tx/" + A]["vout"][0]["scriptpubkey_address"] = '=SUM(1,2),"Synthetic"\nAddress'
        save_json(fixture_path, data)
        limits = Limits(max_hops=2)
        api = Esplora(store, "pending", limits, fixture=fixture_path, min_interval=0)
        state = new_state([A + ":0", B + ":0"], api.base, limits, [], case_id="synthetic-csv-case")
        api.run_id = state["run_id"]
        self.state = trace(api, state, limits, self.root / "checkpoint" / "trace.json")
        export_run(store, self.state, self.archive)
        self.graph = build_graph(self.state)

    def test_bundle_preserves_full_ids_table_headers_archive_and_provenance(self):
        from liquid_tracer.transaction_csv import TRANSACTION_CSV_FIELDS
        before = {str(path.relative_to(self.archive)): path.read_bytes()
                  for path in self.archive.rglob("*") if path.is_file()}
        result = export_csv(self.graph, self.archive, self.destination)
        self.assertEqual(set(result), {"directory", "files"})
        self.assertEqual(result["directory"], str(self.destination.resolve()))
        self.assertEqual([Path(path).name for path in result["files"]], ["transactions.csv"])
        fields, rows = read_csv(self.destination / "transactions.csv")
        self.assertEqual(tuple(fields), TRANSACTION_CSV_FIELDS)
        self.assertEqual(len(rows), len(self.graph["edges"]))
        self.assertIn(A, {row["Transaction Hash"] for row in rows})
        self.assertEqual({p.name for p in self.destination.glob("*.csv")}, {"transactions.csv"})
        info = json.loads((self.destination / "export.json").read_text())
        self.assertEqual(info["row_count"], len(rows))
        self.assertEqual(info["format"], "transaction_io")
        self.assertEqual(info["source_trace_sha256"], digest(before["trace.json"]))
        self.assertEqual(info["source_files"], {name: digest(before[name]) for name in DETAIL_FILES})
        manifest = (self.destination / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(manifest), 2)
        for line in manifest:
            checksum, name = line.split("  ", 1)
            self.assertEqual(checksum, digest((self.destination / name).read_bytes()))
        self.assertFalse((self.destination / "SHA256SUMS.tmp").exists())
        self.assertEqual(before, {str(path.relative_to(self.archive)): path.read_bytes()
                                 for path in self.archive.rglob("*") if path.is_file()})

    def test_interrupted_final_manifest_write_does_not_publish_completion_name(self):
        before = {str(path.relative_to(self.archive)): path.read_bytes()
                  for path in self.archive.rglob("*") if path.is_file()}
        write_text = Path.write_text

        def interrupt(path, text, *args, **kwargs):
            if path.name in ("SHA256SUMS", "SHA256SUMS.tmp"):
                write_text(path, text[:20], *args, **kwargs)
                raise KeyboardInterrupt
            return write_text(path, text, *args, **kwargs)

        with patch.object(Path, "write_text", new=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                export_csv(self.graph, self.archive, self.destination)
        self.assertFalse((self.destination / "SHA256SUMS").exists())
        self.assertEqual((self.destination / "SHA256SUMS.tmp").stat().st_size, 20)
        self.assertTrue((self.destination / "export.json").is_file())
        self.assertEqual(before, {str(path.relative_to(self.archive)): path.read_bytes()
                                 for path in self.archive.rglob("*") if path.is_file()})

    def test_current_graph_fee_setting_does_not_remove_raw_fee_evidence(self):
        before = (self.archive / "outputs.csv").read_bytes()
        for fees in (False, True):
            graph = build_graph(self.state, include_fees=fees)
            destination = self.root / ("fees-shown" if fees else "fees-hidden")
            export_csv(graph, self.archive, destination)
            rows = read_csv(destination / "transactions.csv")[1]
            self.assertEqual(len(rows), len(graph["edges"]))
            self.assertEqual(any("FEE" in row["Address Flags"] for row in rows), fees)
            self.assertEqual((self.archive / "outputs.csv").read_bytes(), before)
            self.assertTrue(any(row["kind"] == "fee" for row in read_csv(self.archive / "outputs.csv")[1]))

    def test_csv_quoting_and_formula_protection_preserve_literal_graph_text(self):
        graph = copy.deepcopy(self.graph)
        graph["nodes"][0]["label"] = '=SUM(1,2),"Synthetic"\nUnicode café'
        graph["edges"][0]["quantity"] = '@SUM(1,2),"Synthetic"\n??'
        export_csv(graph, self.archive, self.destination)
        rows = read_csv(self.destination / "transactions.csv")[1]
        output = next(row for row in rows if row["Transaction Hash"] == A
                      and row["Direction"] == "OUT" and row["Number of I/O"] == "0")
        self.assertEqual(output["Address Hash"], '\'=SUM(1,2),"Synthetic"\nAddress')
        self.assertEqual(output["Asset Value"], "1000000")

    def test_each_reused_file_and_trace_must_have_a_manifest_entry(self):
        manifest = self.archive / "SHA256SUMS"
        original = manifest.read_text()
        for name in ("trace.json", *DETAIL_FILES):
            with self.subTest(missing=name):
                manifest.write_text("".join(line + "\n" for line in original.splitlines()
                                             if line.split("  ", 1)[1] != name))
                with self.assertRaisesRegex(TraceError, "missing required CSV source"):
                    export_csv(self.graph, self.archive, self.destination)
                self.assertFalse(self.destination.parent.exists())
        manifest.write_text(original)

    def test_tampering_is_rejected_before_destination_creation(self):
        for name in ("trace.json", *DETAIL_FILES):
            with self.subTest(tampered=name):
                path = self.archive / name
                original = path.read_bytes()
                path.write_bytes(original + b"tampered")
                with self.assertRaisesRegex(TraceError, "checksum mismatch"):
                    export_csv(self.graph, self.archive, self.destination)
                self.assertFalse(self.destination.parent.exists())
                path.write_bytes(original)

    def test_missing_manifest_duplicate_entry_and_symlink_source_are_rejected(self):
        manifest = self.archive / "SHA256SUMS"
        original = manifest.read_text()
        manifest.unlink()
        with self.assertRaisesRegex(TraceError, "Cannot read saved-run"):
            export_csv(self.graph, self.archive, self.destination)
        manifest.write_text(original + original.splitlines()[0] + "\n")
        with self.assertRaisesRegex(TraceError, "duplicated entry"):
            export_csv(self.graph, self.archive, self.destination)
        manifest.write_text(original)
        source = self.archive / "inputs.csv"
        moved = self.root / "moved-inputs.csv"
        source.rename(moved)
        source.symlink_to(moved)
        with self.assertRaisesRegex(TraceError, "invalid saved CSV source"):
            export_csv(self.graph, self.archive, self.destination)
        self.assertFalse(self.destination.parent.exists())

    def test_existing_file_directory_and_dangling_symlink_are_not_clobbered(self):
        directory = self.root / "existing-directory"
        directory.mkdir()
        marker = directory / "marker"
        marker.write_bytes(b"original")
        regular = self.root / "existing-file"
        regular.write_bytes(b"original")
        link = self.root / "existing-symlink"
        target = self.root / "not-created"
        link.symlink_to(target)
        for destination in (directory, regular, link):
            with self.subTest(destination=destination.name):
                with self.assertRaisesRegex(TraceError, "already exists"):
                    export_csv(self.graph, self.archive, destination)
        self.assertEqual(list(directory.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b"original")
        self.assertEqual(regular.read_bytes(), b"original")
        self.assertTrue(link.is_symlink())
        self.assertFalse(target.exists())

    def test_graph_from_different_run_or_case_is_rejected(self):
        graph = copy.deepcopy(self.graph)
        graph["run_id"] = "different-synthetic-run"
        with self.assertRaisesRegex(TraceError, "does not match the saved run"):
            export_csv(graph, self.archive, self.destination)
        graph = copy.deepcopy(self.graph)
        graph["namespace"]["case_id"] = "different-synthetic-case"
        with self.assertRaisesRegex(TraceError, "does not match the saved case"):
            export_csv(graph, self.archive, self.destination)
        self.assertFalse(self.destination.parent.exists())

    def test_bundle_cannot_be_created_inside_saved_archive(self):
        destination = self.archive / "csv-bundle"
        with self.assertRaisesRegex(TraceError, "outside the saved run"):
            export_csv(self.graph, self.archive, destination)
        self.assertFalse(destination.exists())
