import contextlib
import copy
import csv
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main, parser, refresh_presentation, sync_run, verify_export
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.export import PRESENTATION_VERSION, build_graph
from liquid_tracer.investigations import update_case
from liquid_tracer.miro import make_plan
from tests.fixtures import A, fixture


# Keep the declared local layout tool available while removing credentials.
NODE_ENV = {"LIQUID_NODE_BIN": os.environ.get("LIQUID_NODE_BIN") or shutil.which("node") or ""}

class LayoutCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.case = self.root / "case"
        self.fixture = self.root / "synthetic.json"
        save_json(self.fixture, fixture())
        self.trace_arguments = ["trace", "--case", str(self.case), "--fixture", str(self.fixture)]

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(arguments)
        self.assertEqual(status, 0, errors.getvalue() or output.getvalue())
        return output.getvalue()

    def start(self, *options):
        report = json.loads(self.invoke(self.trace_arguments + ["--seed", A + ":0", "--hops", "1", *options]))
        self.run = Path(report["directory"])
        return report

    def set_fees(self, enabled):
        metadata = read_json(self.case / "case.json")
        update_case(self.case, {"run_defaults": {**metadata.get("run_defaults", {}), "include_fees": enabled}})

    def install_plan(self, plan):
        plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
        save_json(self.run / "miro-plan.json", plan)
        manifest = self.run / "SHA256SUMS"
        names = [line.split("  ", 1)[1] for line in manifest.read_text().splitlines()]
        manifest.write_text("".join(digest((self.run / name).read_bytes()) + "  " + name + "\n" for name in names))

    def test_fee_flags_have_three_states_and_are_mutually_exclusive(self):
        for arguments in (["trace", "--case", "case"],
                          ["export", "--case", "case", "--run", "latest", "--out", "new"],
                          ["miro-sync", "--case", "case"]):
            with self.subTest(command=arguments[0]):
                self.assertIsNone(parser().parse_args(arguments).include_fees)
                self.assertTrue(parser().parse_args(arguments + ["--include-fees"]).include_fees)
                self.assertFalse(parser().parse_args(arguments + ["--exclude-fees"]).include_fees)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parser().parse_args(arguments + ["--include-fees", "--exclude-fees"])

    def test_default_hides_fee_presentation_but_retains_evidence(self):
        self.start()
        state, graph = (read_json(self.run / name) for name in ("trace.json", "graph.json"))
        self.assertEqual(state["graph_options"], {"include_fees": False})
        self.assertEqual(graph["graph_options"], {"include_fees": False})
        self.assertTrue(any(out.get("scriptpubkey_type") == "fee"
                            for tx in state["transactions"].values() for out in tx["data"]["vout"]))
        for filename in ("outputs.csv", "events.csv"):
            with (self.run / filename).open() as stream:
                self.assertTrue(any(row["kind"] == "fee" for row in csv.DictReader(stream)))
        ids = {node["id"] for node in graph["nodes"]} | {edge["id"] for edge in graph["edges"]}
        self.assertTrue(graph["fee_items"])
        self.assertFalse(ids & graph["fee_items"].keys())
        verify_export(self.run)

    def test_continuation_uses_current_case_display_setting_and_flags_are_one_shot(self):
        self.start("--include-fees")
        original = self.snapshot(self.run)
        self.set_fees(False)
        before = read_json(self.case / "case.json")
        report = json.loads(self.invoke(self.trace_arguments + ["--resume", "latest", "--additional-hops", "1"]))
        continued = read_json(Path(report["directory"]) / "trace.json")
        self.assertFalse(continued["graph_options"]["include_fees"])
        self.assertEqual(self.snapshot(self.run), original)
        self.set_fees(True)
        next_run = json.loads(self.invoke(self.trace_arguments + ["--resume", "latest", "--exclude-fees"]))
        self.assertFalse(read_json(Path(next_run["directory"]) / "graph.json")["graph_options"]["include_fees"])
        self.assertTrue(read_json(self.case / "case.json")["run_defaults"]["include_fees"])
        self.assertEqual(before["case_id"], continued["case_id"])

    def test_export_current_fee_choice_and_override_leave_original_archive_intact(self):
        self.start()
        original = self.snapshot(self.run)
        self.set_fees(True)
        for flag, expected in (([], True), (["--exclude-fees"], False)):
            with self.subTest(expected=expected):
                destination = self.root / ("with-fees" if expected else "without-fees")
                self.invoke(["export", "--case", str(self.case), "--run", "latest", "--out", str(destination), *flag])
                graph, state = (read_json(destination / name) for name in ("graph.json", "trace.json"))
                self.assertIs(graph["graph_options"]["include_fees"], expected)
                self.assertIs(state["graph_options"]["include_fees"], expected)
                self.assertEqual(state["transactions"], read_json(self.run / "trace.json")["transactions"])
                verify_export(destination)
        self.assertEqual(self.snapshot(self.run), original)

    def test_sync_reincludes_proven_fees_from_archive_without_refetch_or_local_writes(self):
        self.start()
        self.set_fees(True)
        before = self.snapshot(self.case)
        with patch.dict(os.environ, NODE_ENV, clear=True), \
                patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must not retrace")), \
                patch("liquid_tracer.cli.sync", return_value={"dry_run": True}) as sync:
            report = sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True)
        plan = sync.call_args.args[0]
        self.assertTrue(report["include_fees"])
        self.assertEqual(report["presentation_version"], 6)
        self.assertFalse(report["reorganize"])
        self.assertNotIn("reorganize", sync.call_args.kwargs)
        shape_ids = {item["key"] for item in plan["shapes"]}
        self.assertTrue(any(key in shape_ids for key in plan["fee_items"]))
        self.assertEqual(self.snapshot(self.case), before)

    def test_old_all_fee_plan_refreshes_to_hidden_fees_without_changing_other_topology(self):
        self.start()
        state = read_json(self.run / "trace.json")
        old = make_plan(build_graph(state, include_fees=True))
        old["presentation_version"] = 2
        old.pop("fee_items", None)
        old.pop("graph_options", None)
        old.pop("include_fees", None)
        self.install_plan(old)
        before = self.snapshot(self.run)
        with patch("liquid_tracer.cli.sync", return_value={"dry_run": True}) as sync:
            report = sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True)
        refreshed = sync.call_args.args[0]
        self.assertFalse(report["include_fees"])
        self.assertTrue(report["presentation_refreshed"])
        for category in ("shapes", "connectors"):
            old_keys = {item["key"] for item in old[category]}
            new_keys = {item["key"] for item in refreshed[category]}
            self.assertTrue(new_keys < old_keys)
            self.assertTrue((old_keys - new_keys) <= refreshed["fee_items"].keys())
        self.assertEqual(self.snapshot(self.run), before)
        verify_export(self.run)

    def test_reorganize_is_forwarded_only_when_requested_and_reported(self):
        self.start()
        with patch("liquid_tracer.cli.sync", return_value={"created": 0}) as sync:
            report = json.loads(self.invoke(["miro-sync", "--case", str(self.case), "--board", "SYNTHETIC=",
                                            "--reorganize", "--include-fees"]))
        self.assertEqual(len(sync.call_args_list), 2)
        self.assertTrue(all(call.kwargs["reorganize"] for call in sync.call_args_list))
        self.assertTrue(report["reorganize"])
        self.assertTrue(report["include_fees"])
        self.assertTrue(read_json(Path(report["report_file"]))["reorganize"])

    def test_explicit_plan_fee_override_fails_locally_before_reading_or_syncing(self):
        with patch("liquid_tracer.cli.sync") as sync:
            for option in (False, True):
                with self.subTest(include_fees=option), self.assertRaisesRegex(TraceError, "--plan cannot be combined"):
                    sync_run(self.case, "latest", "SYNTHETIC=", plan_path=self.root / "missing-plan.json",
                             include_fees=option)
        sync.assert_not_called()

    def test_fee_exemption_does_not_allow_renamed_shapes_or_changed_connector_endpoints(self):
        self.start("--include-fees")
        original = read_json(self.run / "miro-plan.json")
        changed_shape = copy.deepcopy(original)
        fee = next(item for item in changed_shape["shapes"] if item["key"] in original["fee_items"])
        fee["body"]["data"]["shape"] = "circle"
        changed_edge = copy.deepcopy(original)
        edge = next(item for item in changed_edge["connectors"] if item["key"] in original["fee_items"])
        edge["source"] = next(item["key"] for item in original["shapes"] if item["key"].startswith("tx:")
                              and item["key"] != edge["source"])
        renamed = copy.deepcopy(original)
        shape = next(item for item in renamed["shapes"] if item["key"] in original["fee_items"])
        shape["key"] = "event:unproven"
        for altered in (changed_shape, changed_edge, renamed):
            with self.subTest(change=altered):
                with self.assertRaisesRegex(TraceError, "topology"):
                    refresh_presentation(altered, self.run / "trace.json", include_fees=False)


if __name__ == "__main__":
    unittest.main()
