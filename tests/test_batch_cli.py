import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main
from liquid_tracer.common import read_json, save_json
from tests.fixtures import A, B, fixture


class BatchInspectionCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.fixture_path = self.root / "synthetic.json"
        save_json(self.fixture_path, fixture())

    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(["inspect-txs", *arguments])
        return status, output.getvalue(), errors.getvalue()

    def test_batch_report_uses_exact_selected_transactions_and_numeric_outputs(self):
        report = self.root / "batch.json"
        status, output, errors = self.invoke("--txids", B.upper() + ", " + A + ", " + B,
                                             "--fixture", str(self.fixture_path), "--output", str(report))
        self.assertEqual((status, output, errors), (0, "Transaction outputs saved.\n", ""))
        result = read_json(report)
        self.assertEqual([tx["txid"] for tx in result["transactions"]], [B, A])
        self.assertEqual(result["transactions"][0]["outputs"][1]["outpoint"], B + ":1")
        self.assertEqual(result["transactions"][1]["outputs"][0]["outpoint"], A + ":0")
        self.assertEqual(set(self.root.iterdir()), {self.fixture_path, report})
        status, output, errors = self.invoke("--txids", B + "," + A, "--fixture", str(self.fixture_path))
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(json.loads(output), result)

    def test_invalid_later_hash_is_rejected_before_filesystem_or_lookup(self):
        with patch("pathlib.Path.exists") as exists, patch("pathlib.Path.is_symlink") as is_symlink, \
                patch("pathlib.Path.is_dir") as is_dir, patch("liquid_tracer.cli.inspect_transactions") as inspect:
            status, output, errors = self.invoke("--txids", A + ", accidentally-pasted-secret", "--output", "unused.json")
        self.assertEqual((status, output), (1, ""))
        self.assertIn("Transaction hash 2", errors)
        self.assertNotIn("accidentally-pasted-secret", errors)
        exists.assert_not_called()
        is_symlink.assert_not_called()
        is_dir.assert_not_called()
        inspect.assert_not_called()

    def test_output_preflight_and_exclusive_creation_preserve_existing_files(self):
        report = self.root / "existing.json"
        report.write_text("preserve evidence")
        for destination in (report, self.root / "missing" / "batch.json"):
            with self.subTest(destination=destination), patch("liquid_tracer.cli.inspect_transactions") as inspect:
                status, output, _ = self.invoke("--txids", A + "," + B, "--output", str(destination))
                self.assertEqual((status, output), (1, ""))
                inspect.assert_not_called()
        self.assertEqual(report.read_text(), "preserve evidence")
        racing_report = self.root / "racing.json"
        def lookup(*args, **kwargs):
            racing_report.write_text("concurrent evidence")
            return {"transactions": []}
        with patch("liquid_tracer.cli.inspect_transactions", side_effect=lookup):
            status, output, errors = self.invoke("--txids", A + "," + B, "--output", str(racing_report))
        self.assertEqual((status, output), (1, ""))
        self.assertIn("already exists", errors)
        self.assertEqual(racing_report.read_text(), "concurrent evidence")

    def test_failed_later_transaction_does_not_write_partial_report(self):
        data = fixture()
        del data["/tx/" + B]
        save_json(self.fixture_path, data)
        report = self.root / "batch.json"
        status, output, errors = self.invoke("--txids", A + "," + B, "--fixture", str(self.fixture_path),
                                             "--output", str(report))
        self.assertEqual((status, output), (1, ""))
        self.assertIn(B, errors)
        self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
