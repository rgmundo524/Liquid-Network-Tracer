import contextlib
import csv
import fcntl
import functools
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import board_id, main, parser
from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.miro import sync as real_sync
from tests.test_miro_sync import FakeMiro


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.case = Path(self.temp.name) / "case"
        self.project = Path(__file__).resolve().parents[1]
        self.base = ["trace", "--case", str(self.case), "--fixture", str(self.project / "examples/demo-api.json")]

    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(arguments)
        return status, output.getvalue(), errors.getvalue()

    def start(self, *arguments):
        status, output, errors = self.invoke(self.base + ["--seeds-file", str(self.project / "examples/demo-seeds.txt"), *arguments])
        self.assertEqual(status, 0, errors or output)
        return json.loads(output)

    def test_trace_continue_csv_identity_and_read_only_preview(self):
        first = self.start("--hops", "1")
        metadata = read_json(self.case / "case.json")
        self.assertEqual(metadata["latest_run"], first["run_id"])
        metadata["analyst_note"] = "Preserve case metadata"
        save_json(self.case / "case.json", metadata)
        first_dir = Path(first["directory"])
        original = {str(p.relative_to(first_dir)): digest(p.read_bytes()) for p in first_dir.rglob("*") if p.is_file()}
        self.assertFalse((first_dir / "graph.html").exists())
        status, output, errors = self.invoke(self.base + ["--resume", "latest", "--additional-hops", "2"])
        self.assertEqual(status, 0, errors)
        second = json.loads(output)
        self.assertEqual(read_json(self.case / "case.json"), {**metadata, "latest_run": second["run_id"]})
        second_dir = Path(second["directory"])
        self.assertEqual(read_json(second_dir / "trace.json")["parent_run"], first["run_id"])
        before, after = (read_json(path / "graph.json") for path in (first_dir, second_dir))
        self.assertEqual(before["namespace"], after["namespace"])
        self.assertTrue({n["id"] for n in before["nodes"]}.issubset({n["id"] for n in after["nodes"]}))
        with (second_dir / "nodes.csv").open() as stream:
            nodes = list(csv.DictReader(stream))
        with (second_dir / "edges.csv").open() as stream:
            edges = list(csv.DictReader(stream))
        keys = {n["id"] for n in nodes}
        self.assertTrue(all(e["source"] in keys and e["target"] in keys for e in edges))
        self.assertEqual(second["stats"]["transactions_cumulative"], 4)
        self.assertEqual(original, {str(p.relative_to(first_dir)): digest(p.read_bytes()) for p in first_dir.rglob("*") if p.is_file()})
        snapshot = {str(p.relative_to(self.case)): digest(p.read_bytes()) for p in self.case.rglob("*") if p.is_file()}
        with patch.dict(os.environ, {}, clear=True):
            status, output, errors = self.invoke(["miro-sync", "--case", str(self.case), "--run", second["run_id"],
                                                "--board", "https://miro.com/app/board/DEMO=/?moveToWidget=42", "--dry-run"])
        self.assertEqual(status, 0, errors)
        self.assertTrue(json.loads(output)["dry_run"])
        self.assertGreater(json.loads(output)["new_items"], 0)
        self.assertEqual(snapshot, {str(p.relative_to(self.case)): digest(p.read_bytes()) for p in self.case.rglob("*") if p.is_file()})
        for directory in (first_dir, second_dir):
            for line in (directory / "SHA256SUMS").read_text().splitlines():
                checksum, name = line.split("  ", 1)
                self.assertEqual(checksum, digest((directory / name).read_bytes()))

    def test_auto_sync_failure_retains_run_and_retry_uses_saved_plan(self):
        with patch.dict(os.environ, {"MIRO_ACCESS_TOKEN": "synthetic-test-token"}), patch("liquid_tracer.cli.sync", side_effect=TraceError("simulated unavailable")):
            status, output, errors = self.invoke(self.base + ["--seeds-file", str(self.project / "examples/demo-seeds.txt"), "--hops", "1", "--miro-board", "DEMO="])
        self.assertEqual(status, 1, errors)
        saved = json.loads(output)
        self.assertEqual(saved["miro_error"], "simulated unavailable")
        self.assertTrue((Path(saved["directory"]) / "SHA256SUMS").exists())
        self.assertEqual(read_json(self.case / "case.json")["latest_run"], saved["run_id"])
        with patch("liquid_tracer.cli.sync", return_value={"created": 10, "conflicts": []}) as sync, patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must not refetch blockchain data")):
            status, output, errors = self.invoke(["miro-sync", "--case", str(self.case), "--board", "DEMO="])
        self.assertEqual(status, 0, errors)
        self.assertEqual(sync.call_args.args[0]["run_id"], saved["run_id"])
        self.assertTrue(Path(json.loads(output)["report_file"]).exists())

    def test_auto_sync_requires_token_before_explorer(self):
        with patch.dict(os.environ, {}, clear=True), patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("No credits should be consumed")):
            status, _, errors = self.invoke(self.base + ["--seed", "a" * 64 + ":0", "--miro-board", "DEMO="])
        self.assertEqual(status, 1)
        self.assertIn("MIRO_ACCESS_TOKEN", errors)
        self.assertFalse(self.case.exists())

    def test_merge_mode_inherits_on_continuation(self):
        first = self.start("--hops", "0", "--merge-addresses")
        status, output, errors = self.invoke(self.base + ["--resume", first["run_id"], "--additional-hops", "1"])
        self.assertEqual(status, 0, errors)
        plan = read_json(Path(json.loads(output)["directory"]) / "miro-plan.json")
        self.assertEqual(plan["namespace"]["address_mode"], "merged")

    def test_board_url_validation(self):
        self.assertEqual(board_id("https://miro.com/app/board/uXjTEST%3D/"), "uXjTEST=")
        for value in ("", "https://evil.example/app/board/TEST/", "https://miro.com@evil.example/app/board/TEST/", "../TEST", "https://miro.com/app/board/TEST%2Fother/"):
            with self.assertRaises(TraceError):
                board_id(value)

    def test_modified_saved_trace_is_rejected_before_continuation(self):
        first = self.start("--hops", "1")
        checkpoint = Path(first["directory"]) / "trace.json"
        checkpoint.write_text(checkpoint.read_text() + "\n")
        with patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must verify evidence before API use")):
            status, _, errors = self.invoke(self.base + ["--resume", first["run_id"]])
        self.assertEqual(status, 1)
        self.assertIn("checksum mismatch", errors)

    def test_saved_demo_runs_incrementally_sync_to_same_board(self):
        first = self.start("--hops", "1")
        remote = FakeMiro()
        adapter = functools.partial(real_sync, token="synthetic-test-token", transport=remote, interval=0)
        command = ["miro-sync", "--case", str(self.case), "--board", "DEMO="]
        with patch("liquid_tracer.cli.sync", adapter):
            status, output, errors = self.invoke(command + ["--run", first["run_id"]])
            self.assertEqual(status, 0, errors)
            saved = read_json(Path(json.loads(output)["state_file"]))
            ids = {key: value["id"] for key, value in saved["items"].items()}
            node = next(remote.items[item] for key, item in ids.items() if key.startswith("liquid:outpoint:"))
            node["position"]["y"] = 5555
            node["data"]["content"] += "<p>Analyst note</p>"
            content = node["data"]["content"]
            status, output, errors = self.invoke(self.base + ["--resume", first["run_id"], "--additional-hops", "2"])
            self.assertEqual(status, 0, errors)
            second = json.loads(output)
            status, output, errors = self.invoke(command + ["--run", second["run_id"]])
            self.assertEqual(status, 0, errors)
            report = json.loads(output)
            self.assertGreater(report["created"], 0)
            self.assertEqual(node["position"]["y"], 5555)
            self.assertEqual(node["data"]["content"], content)
            updated = read_json(Path(report["state_file"]))
            self.assertTrue(all(updated["items"][key]["id"] == item for key, item in ids.items()))
            status, output, errors = self.invoke(command + ["--run", second["run_id"], "--max-new-items", "0"])
            self.assertEqual(status, 0, errors)
            self.assertEqual(json.loads(output)["created"], 0)

    def test_environment_defaults_and_explicit_overrides(self):
        with patch.dict(os.environ, {"LIQUID_CASE_DIR": str(self.case), "LIQUID_MIRO_BOARD": "DEFAULT="}, clear=True):
            args = parser().parse_args(["miro-sync", "--dry-run"])
            self.assertEqual((args.case, args.board, args.run), (self.case, "DEFAULT=", "latest"))
            args = parser().parse_args(["miro-sync", "--case", "another-case", "--board", "OVERRIDE=", "--run", "a" * 16])
            self.assertEqual((args.case, args.board, args.run), (Path("another-case"), "OVERRIDE=", "a" * 16))
            args = parser().parse_args(["export", "--run", "latest", "--out", "new-export"])
            self.assertEqual(args.case, self.case)
            args = parser().parse_args(["trace", "--seed", "a" * 64 + ":0"])
            self.assertEqual(args.case, self.case)
            self.assertIsNone(args.miro_board)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["export", "--out", "new-export"])
            with patch("liquid_tracer.cli.sync", side_effect=AssertionError("A board default must not cause automatic publishing")):
                status, output, errors = self.invoke(["trace", "--fixture", str(self.project / "examples/demo-api.json"),
                    "--seeds-file", str(self.project / "examples/demo-seeds.txt"), "--hops", "0"])
            self.assertEqual(status, 0, errors or output)
        for environment in ({}, {"LIQUID_CASE_DIR": "", "LIQUID_MIRO_BOARD": ""}):
            with patch.dict(os.environ, environment, clear=True):
                for arguments in (["trace"], ["export", "--run", "latest", "--out", "export"],
                                  ["miro-sync", "--board", "DEMO="], ["miro-sync", "--case", str(self.case)]):
                    with self.subTest(environment=environment, arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        parser().parse_args(arguments)

    def test_fresh_process_uses_saved_latest_without_credentials_or_writes(self):
        first = self.start("--hops", "1")
        snapshot = {str(p.relative_to(self.case)): digest(p.read_bytes()) for p in self.case.rglob("*") if p.is_file()}
        result = subprocess.run([sys.executable, "-m", "liquid_tracer.cli", "miro-sync", "--dry-run"],
            cwd=self.project, env={"LIQUID_CASE_DIR": str(self.case), "LIQUID_MIRO_BOARD": "DEMO="},
            text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["run_id"], first["run_id"])
        self.assertTrue(report["dry_run"])
        self.assertGreater(report["new_items"], 0)
        self.assertEqual(snapshot, {str(p.relative_to(self.case)): digest(p.read_bytes()) for p in self.case.rglob("*") if p.is_file()})

    def test_export_latest_preserves_pointer_and_exports_selected_run(self):
        first = self.start("--hops", "1")
        metadata = (self.case / "case.json").read_bytes()
        destination = Path(self.temp.name) / "export"
        with patch.dict(os.environ, {"LIQUID_CASE_DIR": str(self.case)}, clear=True):
            status, _, errors = self.invoke(["export", "--run", "latest", "--out", str(destination)])
        self.assertEqual(status, 0, errors)
        self.assertEqual(read_json(destination / "trace.json")["run_id"], first["run_id"])
        self.assertEqual((self.case / "case.json").read_bytes(), metadata)

    def test_failed_export_does_not_advance_latest(self):
        first = self.start("--hops", "1")
        metadata = (self.case / "case.json").read_bytes()

        def fail_export(store, state, destination, *arguments):
            (destination / "nodes.csv").write_text("partial export\n")
            raise OSError("simulated export failure")

        with patch("liquid_tracer.cli.export_run", side_effect=fail_export):
            status, _, errors = self.invoke(self.base + ["--resume", "latest", "--additional-hops", "1"])
        self.assertEqual(status, 1)
        self.assertIn("simulated export failure", errors)
        self.assertEqual((self.case / "case.json").read_bytes(), metadata)
        self.assertEqual(read_json(self.case / "case.json")["latest_run"], first["run_id"])
        self.assertEqual(len(list((self.case / "runs").iterdir())), 2)

    def test_latest_errors_do_not_fall_back_to_other_runs(self):
        first = self.start("--hops", "1")
        second = self.start("--hops", "2")
        metadata = read_json(self.case / "case.json")
        command = ["miro-sync", "--case", str(self.case), "--board", "DEMO=", "--dry-run"]
        for pointer, expected in ((None, "No latest run"), (123, "Invalid latest_run"),
                                  ("../../invalid", "Invalid latest_run"), ("f" * 16, "no SHA256SUMS")):
            broken = dict(metadata)
            if pointer is None:
                del broken["latest_run"]
            else:
                broken["latest_run"] = pointer
            save_json(self.case / "case.json", broken)
            with self.subTest(pointer=pointer):
                status, _, errors = self.invoke(command)
                self.assertEqual(status, 1)
                self.assertIn(expected, errors)
                status, _, errors = self.invoke(command + ["--run", first["run_id"]])
                self.assertEqual(status, 0, errors)
        save_json(self.case / "case.json", metadata)
        checkpoint = Path(second["directory"]) / "trace.json"
        checkpoint.write_text(checkpoint.read_text() + "\n")
        status, _, errors = self.invoke(command)
        self.assertEqual(status, 1)
        self.assertIn("checksum mismatch", errors)
        with patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must verify latest before API use")):
            status, _, errors = self.invoke(self.base + ["--resume", "latest"])
        self.assertEqual(status, 1)
        self.assertIn("checksum mismatch", errors)

    def test_latest_requires_matching_case_identity(self):
        self.start("--hops", "1")
        metadata = read_json(self.case / "case.json")
        metadata["case_id"] = "f" * 32
        save_json(self.case / "case.json", metadata)
        with patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must verify case before API use")):
            status, _, errors = self.invoke(self.base + ["--resume", "latest"])
        self.assertEqual(status, 1)
        self.assertIn("different case", errors)
        status, _, errors = self.invoke(["miro-sync", "--case", str(self.case), "--board", "DEMO=", "--dry-run"])
        self.assertEqual(status, 1)
        self.assertIn("matching case identity", errors)

    def test_latest_resume_resolution_happens_after_trace_lock(self):
        self.start("--hops", "1")
        with (self.case / "trace.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("liquid_tracer.cli.resolve_latest", side_effect=AssertionError("Must acquire lock before selecting a parent")):
                status, _, errors = self.invoke(self.base + ["--resume", "latest"])
        self.assertEqual(status, 1)
        self.assertIn("Another trace is running", errors)

    def test_paused_and_error_exports_are_saved_as_latest(self):
        paused = self.start("--max-requests", "1")
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(read_json(self.case / "case.json")["latest_run"], paused["run_id"])
        status, output, errors = self.invoke(self.base + ["--seed", "a" * 64 + ":0"])
        self.assertEqual(status, 1, errors)
        failed = json.loads(output)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(read_json(self.case / "case.json")["latest_run"], failed["run_id"])
        self.assertTrue((Path(failed["directory"]) / "SHA256SUMS").exists())


if __name__ == "__main__":
    unittest.main()
