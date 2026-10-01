"""Starting outputs remain visible for the exact selected saved snapshot."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.web import LocalServer


class SeedSummaryWebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.server = LocalServer(root / "cases", root / "assets", port=0)
        self.addCleanup(self.server.server_close)
        self.case = create_investigation(self.server.root, "Seed summary", seeds=["a" * 64 + ":0"])
        self.metadata = read_case(self.case)

    def save_run(self, run_id, **fields):
        directory = self.case / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SHA256SUMS").touch()
        save_json(directory / "trace.json", {"run_id": run_id,
                  "case_id": self.metadata["case_id"], "stats": {}, **fields})
        return directory / "trace.json"

    def test_detail_exposes_each_snapshots_seeds_normalized_without_fetching(self):
        first, second = "1" * 16, "2" * 16
        self.save_run(first, seeds=["B" * 64 + ":010", "b" * 64 + ":2", "B" * 64 + ":0",
                                    "b" * 64 + ":02"])
        self.save_run(second, seeds=["c" * 64 + ":3"])
        self.metadata["latest_run"] = second
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No data fetch")):
            detail = self.server.case_summary(self.case, self.metadata, detail=True)
        runs = {run["id"]: run for run in detail["runs"]}
        self.assertEqual(runs[first]["seeds"], ["b" * 64 + f":{index}" for index in (0, 2, 10)])
        self.assertEqual(runs[second]["seeds"], ["c" * 64 + ":3"])
        self.assertEqual(detail["latest"]["seeds"], runs[second]["seeds"])
        self.assertEqual(detail["seeds"], ["a" * 64 + ":0"])
        self.assertNotIn("seeds", self.server.case_summary(self.case, self.metadata)["latest"])

    def test_missing_empty_and_invalid_selections_remain_distinct(self):
        self.save_run("0" * 16)
        self.save_run("1" * 16, seeds=[])
        invalid = (None, "a" * 64 + ":0", [True], [{"private": "do-not-expose"}],
                   ["a" * 64 + ":0", "<script>do-not-expose</script>"],
                   ["a" * 64 + ":4294967296"], ["a" * 64 + ":-1"])
        for index, value in enumerate(invalid, start=2):
            self.save_run(str(index) * 16, seeds=value)
        detail = self.server.case_summary(self.case, self.metadata, detail=True)
        runs = {run["id"]: run for run in detail["runs"]}
        self.assertEqual(len(runs), len(invalid) + 2)
        self.assertEqual(runs["1" * 16]["seeds"], [])
        for run_id, run in runs.items():
            if run_id != "1" * 16:
                self.assertNotIn("seeds", run)

    def test_legacy_cli_case_reads_snapshot_seeds_without_modifying_evidence(self):
        self.metadata.pop("seeds")
        self.metadata["latest_run"] = "f" * 16
        save_json(self.case / "case.json", self.metadata)
        path = self.save_run("f" * 16, seeds=["d" * 64 + ":0"])
        before = {item: item.read_bytes() for item in (path, self.case / "case.json")}
        detail = self.server.case_summary(self.case, self.metadata, detail=True)
        self.assertEqual(detail["seeds"], [])
        self.assertEqual(detail["latest"]["seeds"], ["d" * 64 + ":0"])
        self.assertEqual({item: item.read_bytes() for item in before}, before)


if __name__ == "__main__":
    unittest.main()
