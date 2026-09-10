import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main, open_preview, verify_export
from liquid_tracer.common import read_json, save_json
from liquid_tracer.investigations import update_case
from tests.fixtures import A, fixture


class MermaidCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = self.root / "case"
        fixture_path = self.root / "fixture.json"
        save_json(fixture_path, fixture())
        status, output, errors = self.invoke(["trace", "--case", str(self.case), "--fixture", str(fixture_path),
                                               "--seed", A + ":0", "--hops", "1", "--merge-addresses"])
        self.assertEqual(status, 0, errors)
        self.run = Path(json.loads(output)["directory"])
        self.arguments = ["mermaid", "--case", str(self.case)]

    @staticmethod
    def invoke(arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(arguments)
        return status, output.getvalue(), errors.getvalue()

    @staticmethod
    def snapshot(directory):
        return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}

    @staticmethod
    def export_stub(graph, directory):
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=False)
        save_json(directory / "graph.json", graph)
        return {"directory": str(directory), "source": str(directory / "graph.mmd"),
                "svg": str(directory / "graph.svg"), "html": str(directory / "graph.html")}

    def test_latest_chart_is_offline_uses_case_settings_and_preserves_archive(self):
        update_case(self.case, {"run_defaults": {"include_fees": True}})
        original = self.snapshot(self.case)
        with patch.dict(os.environ, {}, clear=True), \
                patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must not retrace")), \
                patch("liquid_tracer.cli.sync", side_effect=AssertionError("Must not contact Miro")), \
                patch("liquid_tracer.mermaid.export_mermaid", side_effect=self.export_stub), \
                patch("liquid_tracer.cli.open_preview", return_value=True) as opener:
            status, output, errors = self.invoke(self.arguments + ["--open"])
        self.assertEqual(status, 0, errors)
        result = json.loads(output)
        self.assertEqual(result["run_id"], self.run.name)
        self.assertTrue(result["include_fees"])
        self.assertTrue(result["browser_opened"])
        opener.assert_called_once_with(result["html"])
        self.assertEqual(Path(result["directory"]).parent, self.case / "previews")
        graph = read_json(Path(result["directory"]) / "graph.json")
        self.assertEqual(graph["namespace"]["address_mode"], "merged")
        self.assertTrue(graph["graph_options"]["include_fees"])
        for relative, contents in original.items():
            self.assertEqual((self.case / relative).read_bytes(), contents)
        verify_export(self.run)

    def test_repeated_chart_creates_new_directory_and_fee_override_is_not_saved(self):
        update_case(self.case, {"run_defaults": {"include_fees": True}})
        with patch("liquid_tracer.mermaid.export_mermaid", side_effect=self.export_stub), \
                patch("liquid_tracer.cli.open_preview") as opener:
            results = []
            for _ in range(2):
                status, output, errors = self.invoke(self.arguments + ["--exclude-fees"])
                self.assertEqual(status, 0, errors)
                results.append(json.loads(output))
        self.assertNotEqual(results[0]["directory"], results[1]["directory"])
        self.assertTrue(all(not result["include_fees"] for result in results))
        self.assertTrue(read_json(self.case / "case.json")["run_defaults"]["include_fees"])
        opener.assert_not_called()

    def test_explicit_historical_run_and_output_ignore_latest_pointer(self):
        metadata = read_json(self.case / "case.json")
        metadata["latest_run"] = "0" * 16
        save_json(self.case / "case.json", metadata)
        destination = self.root / "historical-preview"
        with patch("liquid_tracer.mermaid.export_mermaid", side_effect=self.export_stub):
            status, output, errors = self.invoke(self.arguments + ["--run", self.run.name, "--out", str(destination)])
        self.assertEqual(status, 0, errors)
        self.assertEqual(json.loads(output)["directory"], str(destination))

    def test_tampered_archive_rejected_before_rendering(self):
        with (self.run / "trace.json").open("a") as stream:
            stream.write("\n")
        with patch("liquid_tracer.mermaid.export_mermaid") as renderer:
            status, _, errors = self.invoke(self.arguments)
        self.assertEqual(status, 1)
        self.assertIn("checksum mismatch", errors)
        renderer.assert_not_called()
        self.assertFalse((self.case / "previews").exists())

    def test_wrong_case_and_archive_destination_rejected(self):
        with patch("liquid_tracer.mermaid.export_mermaid") as renderer:
            status, _, errors = self.invoke(self.arguments + ["--out", str(self.run / "preview")])
            self.assertEqual(status, 1)
            self.assertIn("outside runs/", errors)
            metadata = read_json(self.case / "case.json")
            metadata["case_id"] = "f" * 32
            save_json(self.case / "case.json", metadata)
            status, _, errors = self.invoke(self.arguments)
            self.assertEqual(status, 1)
            self.assertIn("different case", errors)
        renderer.assert_not_called()

    def test_no_latest_run_rejected_without_creating_preview(self):
        metadata = read_json(self.case / "case.json")
        metadata.pop("latest_run")
        save_json(self.case / "case.json", metadata)
        with patch("liquid_tracer.mermaid.export_mermaid") as renderer:
            status, _, errors = self.invoke(self.arguments)
        self.assertEqual(status, 1)
        self.assertIn("No latest run", errors)
        renderer.assert_not_called()

    def test_browser_failures_leave_completed_chart_successful(self):
        for failure in (OSError("No browser"), subprocess.TimeoutExpired("browser", 10)):
            with self.subTest(failure=failure), patch("liquid_tracer.cli.subprocess.run", side_effect=failure):
                self.assertFalse(open_preview(self.root / "graph.html"))
        with patch("liquid_tracer.mermaid.export_mermaid", side_effect=self.export_stub), \
                patch("liquid_tracer.cli.open_preview", return_value=False):
            status, output, errors = self.invoke(self.arguments + ["--open"])
        self.assertEqual(status, 0, errors)
        self.assertFalse(json.loads(output)["browser_opened"])


if __name__ == "__main__":
    unittest.main()
