import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main, parser, verify_export
from liquid_tracer.common import read_json, save_json
from liquid_tracer.investigations import update_case
from tests.fixtures import A, fixture


class CsvCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = self.root / "case"
        self.fixture = self.root / "fixture.json"
        save_json(self.fixture, fixture())
        self.trace_arguments = ["trace", "--case", str(self.case), "--fixture", str(self.fixture)]
        self.arguments = ["csv-export", "--case", str(self.case)]
        report = self.success(self.trace_arguments + ["--seed", A + ":0", "--hops", "1", "--merge-addresses"])
        self.archive = Path(report["directory"])

    @staticmethod
    def invoke(arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(arguments)
        return status, output.getvalue(), errors.getvalue()

    def success(self, arguments):
        status, output, errors = self.invoke(arguments)
        self.assertEqual(status, 0, errors or output)
        return json.loads(output)

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    @staticmethod
    def rows(path):
        with path.open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))

    def test_latest_export_is_offline_and_works_from_archive_without_database(self):
        (self.case / "evidence.sqlite").unlink()
        before = self.snapshot(self.case)
        with patch.dict(os.environ, {}, clear=True), \
                patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("No API access")), \
                patch("liquid_tracer.cli.Store", side_effect=AssertionError("No database needed")), \
                patch("liquid_tracer.cli.sync", side_effect=AssertionError("No Miro access")):
            result = self.success(self.arguments)
        directory = Path(result["directory"])
        self.assertEqual(directory.parent, self.case / "exports")
        self.assertEqual(result["run_id"], self.archive.name)
        self.assertFalse(result["include_fees"])
        self.assertEqual({Path(path).name for path in result["files"]},
                         {"nodes.csv", "edges.csv", "inputs.csv", "outputs.csv", "spends.csv", "events.csv", "frontier.csv"})
        self.assertTrue(all(Path(path).is_absolute() and Path(path).is_file() for path in result["files"]))
        nodes = self.rows(directory / "nodes.csv")
        edges = self.rows(directory / "edges.csv")
        self.assertIn("tx:" + A, {row["id"] for row in nodes})
        self.assertTrue(any("2023-11-14 UTC" in row["label"] for row in nodes))
        self.assertTrue(all(edge["source"] in {node["id"] for node in nodes}
                            and edge["target"] in {node["id"] for node in nodes} for edge in edges))
        for relative, contents in before.items():
            self.assertEqual((self.case / relative).read_bytes(), contents)
        self.assertFalse((self.case / "evidence.sqlite").exists())
        verify_export(self.archive)

    def test_current_fees_and_one_shot_override_only_affect_graph_tables(self):
        update_case(self.case, {"run_defaults": {"include_fees": True}})
        before = self.snapshot(self.archive)
        included = self.success(self.arguments)
        excluded = self.success(self.arguments + ["--exclude-fees"])
        self.assertNotEqual(included["directory"], excluded["directory"])
        for result, expected in ((included, True), (excluded, False)):
            directory = Path(result["directory"])
            self.assertEqual(result["include_fees"], expected)
            self.assertEqual(any(row["label"].startswith("FEE\n") for row in self.rows(directory / "nodes.csv")), expected)
            self.assertTrue(any(row["kind"] == "fee" for row in self.rows(directory / "outputs.csv")))
            for name in ("inputs.csv", "outputs.csv", "spends.csv", "events.csv", "frontier.csv"):
                self.assertEqual((directory / name).read_bytes(), (self.archive / name).read_bytes())
        self.assertTrue(read_json(self.case / "case.json")["run_defaults"]["include_fees"])
        self.assertEqual(self.snapshot(self.archive), before)

    def test_historical_selection_after_continuation_and_explicit_destination(self):
        latest = self.success(self.trace_arguments + ["--resume", "latest", "--additional-hops", "1"])
        directory = self.root / "historical-csv"
        result = self.success(self.arguments + ["--run", self.archive.name, "--out", str(directory)])
        self.assertEqual(result["run_id"], self.archive.name)
        self.assertEqual(result["directory"], str(directory))
        self.assertEqual((directory / "outputs.csv").read_bytes(), (self.archive / "outputs.csv").read_bytes())
        self.assertEqual(read_json(self.case / "case.json")["latest_run"], latest["run_id"])
        before = self.snapshot(directory)
        status, _, errors = self.invoke(self.arguments + ["--out", str(directory)])
        self.assertEqual(status, 1)
        self.assertIn("already exists", errors)
        self.assertEqual(self.snapshot(directory), before)

    def test_missing_latest_tampered_archive_and_archival_destination_fail_before_writing(self):
        with patch("liquid_tracer.csv_export.export_csv") as exporter:
            status, _, errors = self.invoke(self.arguments + ["--out", str(self.archive / "csv")])
            self.assertEqual(status, 1)
            self.assertIn("outside runs/", errors)
            with (self.archive / "outputs.csv").open("a") as stream:
                stream.write("altered\n")
            status, _, errors = self.invoke(self.arguments)
            self.assertEqual(status, 1)
            self.assertIn("checksum mismatch", errors)
            metadata = read_json(self.case / "case.json")
            metadata.pop("latest_run")
            save_json(self.case / "case.json", metadata)
            status, _, errors = self.invoke(self.arguments)
            self.assertEqual(status, 1)
            self.assertIn("No latest run", errors)
        exporter.assert_not_called()
        self.assertFalse((self.case / "exports").exists())

    def test_case_identity_and_fee_argument_validation(self):
        metadata = read_json(self.case / "case.json")
        metadata["case_id"] = "f" * 32
        save_json(self.case / "case.json", metadata)
        status, _, errors = self.invoke(self.arguments)
        self.assertEqual(status, 1)
        self.assertIn("different case", errors)
        self.assertFalse((self.case / "exports").exists())
        self.assertIsNone(parser().parse_args(self.arguments).include_fees)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(self.arguments + ["--include-fees", "--exclude-fees"])


if __name__ == "__main__":
    unittest.main()
