import contextlib
import copy
import functools
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main, sync_run, verify_export
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.export import PRESENTATION_VERSION, build_graph
from liquid_tracer.miro import sync as real_sync
from tests.fixtures import A, X, fixture
from tests.test_miro_sync import FakeMiro


class PresentationRefreshTests(unittest.TestCase):
    """Old saved exports stay evidence while existing board items get new labels."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.case = root / "case"
        fixture_path = root / "synthetic.json"
        save_json(fixture_path, fixture())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["trace", "--case", str(self.case), "--fixture", str(fixture_path),
                           "--seed", A + ":0", "--hops", "1"])
        self.assertEqual(status, 0, output.getvalue())
        summary = json.loads(output.getvalue())
        self.run_id = summary["run_id"]
        self.run = Path(summary["directory"])
        self.state_path = self.case / "miro" / (digest(b"SYNTHETIC=")[:24] + ".json")
        self.current_plan = read_json(self.run / "miro-plan.json")
        self.old_plan = copy.deepcopy(self.current_plan)
        self.old_plan.pop("presentation_version", None)
        for item in self.old_plan["shapes"]:
            if item["key"] == "legend":
                item["body"]["data"]["content"] = "<p>Red: seed. Yellow: candidate. Gray: context. Asset and amount may be confidential.</p>"
            elif item["body"]["data"]["shape"] == "circle":
                item["body"]["data"]["content"] = "<p>Old address label<br>" + X + ":0</p>"
        for item in self.old_plan["connectors"]:
            item["body"]["captions"][0]["content"] = "vout 0 · amount confidential · asset confidential"
        self.save_old_plan()
        self.remote = FakeMiro()
        self.adapter = functools.partial(real_sync, token="SYNTHETIC-token", transport=self.remote, interval=0)
        self.archive = self.snapshot(self.run)

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def checksums(self):
        manifest = self.run / "SHA256SUMS"
        names = [line.split("  ", 1)[1] for line in manifest.read_text().splitlines()]
        manifest.write_text("".join(digest((self.run / name).read_bytes()) + "  " + name + "\n" for name in names))

    def save_old_plan(self):
        self.old_plan["sha256"] = digest(canonical({k: v for k, v in self.old_plan.items() if k != "sha256"}))
        save_json(self.run / "miro-plan.json", self.old_plan)
        self.checksums()

    def populate_old_board(self):
        self.adapter(self.old_plan, "SYNTHETIC=", self.state_path)
        return {key: record["id"] for key, record in read_json(self.state_path)["items"].items()}

    def test_existing_board_refresh_keeps_ids_manual_edits_and_archived_evidence(self):
        ids = self.populate_old_board()
        address_keys = [item["key"] for item in self.old_plan["shapes"]
                        if item["body"]["data"]["shape"] == "circle"]
        manual = self.remote.items[ids[address_keys[0]]]
        manual["data"]["content"] += "<p>Analyst annotation</p>"
        manual["position"]["y"] = 5500
        manual["geometry"]["width"] = 300
        original_manual = copy.deepcopy(manual)
        connector_key = self.old_plan["connectors"][0]["key"]
        self.remote.items[ids[connector_key]]["captions"][0]["content"] = "Analyst relationship note"
        posts = len([call for call in self.remote.calls if call[0] == "POST"])
        latest = read_json(self.case / "case.json")["latest_run"]
        with patch("liquid_tracer.cli.sync", self.adapter), \
                patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Saved run must not retrace")):
            result = sync_run(self.case, "latest", "SYNTHETIC=", max_new_items=0)
            self.assertEqual(result["created"], 0)
            self.assertGreater(result["updated"], 0)
            self.assertEqual(result["presentation_version"], PRESENTATION_VERSION)
            self.assertTrue(result["presentation_refreshed"])
            self.assertEqual(result["plan_sha256"], self.current_plan["sha256"])
            self.assertEqual(result["archived_plan_sha256"], self.old_plan["sha256"])
            self.assertEqual(read_json(Path(result["report_file"]))["plan_sha256"], self.current_plan["sha256"])
            updated_ids = {key: record["id"] for key, record in read_json(self.state_path)["items"].items()}
            self.assertEqual(updated_ids, ids)
            self.assertEqual(self.remote.items[ids[address_keys[0]]], original_manual)
            self.assertEqual(self.remote.items[ids[connector_key]]["captions"][0]["content"], "Analyst relationship note")
            for shape in self.current_plan["shapes"]:
                if shape["key"] != address_keys[0]:
                    self.assertEqual(self.remote.items[ids[shape["key"]]]["data"]["content"], shape["body"]["data"]["content"])
            for connector in self.current_plan["connectors"]:
                item = self.remote.items[ids[connector["key"]]]
                self.assertEqual((item["startItem"]["id"], item["endItem"]["id"]),
                                 (ids[connector["source"]], ids[connector["target"]]))
                if connector["key"] != connector_key:
                    self.assertEqual(item["captions"], connector["body"]["captions"])
                    self.assertNotIn("confidential", item["captions"][0]["content"])
            writes = len(self.remote.writes)
            repeated = sync_run(self.case, "latest", "SYNTHETIC=", max_new_items=0)
        self.assertEqual((repeated["created"], repeated["updated"]), (0, 0))
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(len([call for call in self.remote.calls if call[0] == "POST"]), posts)
        for method, _, body in self.remote.writes:
            if method == "PATCH":
                self.assertFalse({"position", "geometry", "startItem", "endItem"} & body.keys())
        self.assertEqual(self.snapshot(self.run), self.archive)
        self.assertEqual(read_json(self.case / "case.json")["latest_run"], latest)
        verify_export(self.run)

    def test_preview_refreshes_in_memory_without_credentials_network_or_writes(self):
        self.populate_old_board()
        before = self.snapshot(self.case)
        calls = len(self.remote.calls)
        with patch.dict(os.environ, {}, clear=True), patch("liquid_tracer.cli.sync", self.adapter):
            report = sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertTrue(report["presentation_refreshed"])
        self.assertEqual(report["new_items"], 0)
        self.assertEqual(report["plan_sha256"], self.current_plan["sha256"])
        self.assertEqual(len(self.remote.calls), calls)
        self.assertEqual(self.snapshot(self.case), before)

    def test_explicit_plan_remains_the_reviewed_presentation(self):
        with patch("liquid_tracer.cli.build_graph", side_effect=AssertionError("Explicit plans must remain unchanged")), \
                patch("liquid_tracer.cli.sync", wraps=self.adapter) as sync:
            report = sync_run(self.case, "latest", "SYNTHETIC=", dry_run=True,
                              plan_path=self.run / "miro-plan.json")
        self.assertEqual(sync.call_args.args[0], self.old_plan)
        self.assertFalse(report["presentation_refreshed"])
        self.assertEqual(report["presentation_version"], 1)
        self.assertEqual(self.snapshot(self.run), self.archive)

    def test_modified_evidence_and_bad_plan_checksum_stop_before_sync(self):
        trace_path = self.run / "trace.json"
        trace_path.write_bytes(trace_path.read_bytes() + b"\n")
        with patch("liquid_tracer.cli.sync") as sync, self.assertRaisesRegex(TraceError, "checksum mismatch"):
            sync_run(self.case, "latest", "SYNTHETIC=")
        sync.assert_not_called()
        trace_path.write_bytes(self.archive["trace.json"])
        self.old_plan["sha256"] = "0" * 64
        save_json(self.run / "miro-plan.json", self.old_plan)
        self.checksums()
        with patch("liquid_tracer.cli.sync") as sync, self.assertRaisesRegex(TraceError, "Miro plan checksum mismatch"):
            sync_run(self.case, "latest", "SYNTHETIC=")
        sync.assert_not_called()

    def test_mismatched_trace_identity_stops_before_sync(self):
        original = read_json(self.run / "trace.json")
        for field, value in (("run_id", "a" * 16), ("case_id", "b" * 32),
                             ("source", "fixture://another-source")):
            with self.subTest(field=field):
                save_json(self.run / "trace.json", {**original, field: value})
                self.checksums()
                with patch("liquid_tracer.cli.sync") as sync, self.assertRaisesRegex(TraceError, "does not match"):
                    sync_run(self.case, "latest", "SYNTHETIC=")
                sync.assert_not_called()

    def test_presentation_cannot_add_nodes_change_types_or_move_connector_endpoints(self):
        graph = build_graph(read_json(self.run / "trace.json"))
        added = copy.deepcopy(graph)
        extra = {**added["nodes"][0], "id": "unexpected-node"}
        added["nodes"].append(extra)
        changed_type = copy.deepcopy(graph)
        changed_type["nodes"][0]["kind"] = "address" if graph["nodes"][0]["kind"] == "transaction" else "transaction"
        moved = copy.deepcopy(graph)
        edge = moved["edges"][0]
        edge["source"] = next(node["id"] for node in moved["nodes"]
                              if node["id"] not in (edge["source"], edge["target"]))
        for changed in (added, changed_type, moved):
            with self.subTest(change=changed is added), patch("liquid_tracer.cli.build_graph", return_value=changed), \
                    patch("liquid_tracer.cli.sync") as sync, self.assertRaisesRegex(TraceError, "topology"):
                sync_run(self.case, "latest", "SYNTHETIC=")
            sync.assert_not_called()
        self.assertEqual(self.snapshot(self.run), self.archive)


if __name__ == "__main__":
    unittest.main()
