"""Large count lookups persist only new observations between final exports."""
import copy
import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.address_count_cache import CountCacheJournal, _checksum, _initialize, read_snapshot
from liquid_tracer.address_counts import _cache, _collect_counts, _legacy_cache, _valid, apply_saved_counts
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case


SOURCE = "https://blockstream.info/liquid/api"


class AddressCountCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Incremental counts")
        self.state = {"case_id": read_case(self.case)["case_id"], "run_id": "0123456789abcdef",
                      "source": SOURCE, "transactions": {}, "fetch_options": {"workers": 1}}
        self.address = "SYNTHETIC-first"

    def observation(self, address=None, count=1, source=SOURCE, day=1):
        return {"address": address or self.address, "source": source,
                "observed_at": f"2026-01-{day:02d}T00:00:00Z", "confirmed_tx_count": count,
                "mempool_tx_count": 0, "observation_ids": [f"observation-{day}"]}

    def legacy(self, records, source=SOURCE):
        data = {"schema_version": 1, "case_id": self.state["case_id"], "source": source, "counts": records}
        data["sha256"] = digest(canonical(data))
        save_json(self.case / "address-counts.json", data)

    def journal(self, source=SOURCE):
        journal = CountCacheJournal(self.case, self.state["case_id"], source)
        self.addCleanup(journal.close)
        return journal

    def collect(self, wanted, calls):
        def transport(method, url, headers, body, timeout):
            address = url.rsplit("/", 1)[1]
            calls.append(address)
            return 200, {}, canonical({"address": address, "chain_stats": {"tx_count": 5},
                                      "mempool_stats": {"tx_count": 2}})
        with patch("liquid_tracer.api.default_min_interval", return_value=.000001), \
                patch.dict(os.environ, {"LIQUID_COUNT_WORKERS": "1"}):
            return _collect_counts(self.case, copy.deepcopy(self.state), wanted,
                                   max_requests=0, max_seconds=0, transport=transport)

    def test_read_only_empty_cache_does_not_create_storage(self):
        before = set(self.case.iterdir())
        self.assertEqual(_cache(self.case, self.state), {})
        self.assertEqual(set(self.case.iterdir()), before)

    def test_reader_at_first_publication_does_not_block_initial_writer(self):
        with contextlib.ExitStack() as readers:
            def published(path, case_id):
                _initialize(path, case_id)
                readers.enter_context(read_snapshot(self.case, case_id, SOURCE, _valid))

            with patch("liquid_tracer.address_count_cache._initialize", side_effect=published):
                journal = self.journal()
            journal.write({self.address: self.observation()})
            self.assertEqual(_cache(self.case, self.state)[self.address], self.observation())

    def test_committed_unfinished_lookup_resumes_without_duplicate_requests(self):
        record = self.observation()
        journal = self.journal()
        journal.write({self.address: record})
        journal.close()  # Model process exit without final JSON export.
        self.assertFalse((self.case / "address-counts.json").exists())
        calls = []
        report = self.collect([self.address, "SYNTHETIC-second"], calls)
        self.assertEqual(calls, ["SYNTHETIC-second"])
        self.assertEqual((report["fetched"], report["remaining"]), (1, 0))
        self.assertEqual(_cache(self.case, self.state)[self.address], record)
        with sqlite3.connect(self.case / "address-counts.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)

    def test_final_export_failure_leaves_completed_records_recoverable(self):
        calls = []
        with patch("liquid_tracer.address_counts.save_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.collect([self.address], calls)
        self.assertEqual(_cache(self.case, self.state)[self.address]["confirmed_tx_count"], 5)
        calls.clear()
        report = self.collect([self.address], calls)
        self.assertEqual(calls, [])
        self.assertEqual(report["known"], 1)

    def test_merge_keeps_newest_state_legacy_and_delta_and_observes_later_legacy_updates(self):
        self.legacy({self.address: self.observation(count=1, day=1)})
        journal = self.journal()
        journal.write({self.address: self.observation(count=2, day=2)})
        state = copy.deepcopy(self.state)
        self.assertEqual(apply_saved_counts(self.case, state)[self.address]["confirmed_tx_count"], 2)
        self.legacy({self.address: self.observation(count=3, day=3)})
        self.assertEqual(apply_saved_counts(self.case, state)[self.address]["confirmed_tx_count"], 3)
        state["address_tx_counts"][self.address] = self.observation(count=4, day=4)
        self.assertEqual(apply_saved_counts(self.case, state)[self.address]["confirmed_tx_count"], 4)

    def test_concurrent_compaction_cannot_drop_rows_between_json_read_and_delta_read(self):
        old, new = self.observation(count=1), self.observation(count=2, day=2)
        self.legacy({self.address: old})
        journal = self.journal()
        journal.write({self.address: new})

        def racing_legacy(case, state):
            counts = _legacy_cache(case, state)
            self.legacy({self.address: new})
            journal.compacted()
            return counts

        with patch("liquid_tracer.address_counts._legacy_cache", side_effect=racing_legacy):
            self.assertEqual(_cache(self.case, self.state)[self.address], new)
        self.assertEqual(_cache(self.case, self.state)[self.address], new)

    def test_source_scoping_does_not_reuse_counts_from_another_explorer(self):
        other = "https://another.example/liquid/api"
        self.legacy({self.address: self.observation(source=other)}, source=other)
        journal = self.journal(source=other)
        journal.write({self.address: self.observation(source=other)})
        self.assertEqual(_cache(self.case, self.state), {})
        self.assertEqual(len(_cache(self.case, {**self.state, "source": other})), 1)

    def test_case_mismatch_and_corrupt_row_are_rejected(self):
        journal = self.journal()
        journal.write({self.address: self.observation()})
        with self.assertRaisesRegex(TraceError, "Invalid address count cache"):
            _cache(self.case, {**self.state, "case_id": "another-case"})
        with journal.connection:
            journal.connection.execute("UPDATE observations SET payload = ?", (canonical(self.observation(count=9)),))
        with self.assertRaisesRegex(TraceError, "Invalid address count cache"):
            _cache(self.case, self.state)

    def test_checksums_bind_row_keys_and_invalid_observations_are_rejected(self):
        journal = self.journal()
        record = self.observation()
        journal.write({self.address: record})
        with journal.connection:
            journal.connection.execute("UPDATE observations SET address = 'SYNTHETIC-other'")
        with self.assertRaisesRegex(TraceError, "Invalid address count cache"):
            _cache(self.case, self.state)
        record["confirmed_tx_count"] = -1
        payload = canonical(record)
        checksum = _checksum(self.state["case_id"], SOURCE, self.address, payload)
        with journal.connection:
            journal.connection.execute("UPDATE observations SET address = ?, payload = ?, sha256 = ?",
                                       (self.address, payload, checksum))
        with self.assertRaisesRegex(TraceError, "Invalid address transaction count"):
            _cache(self.case, self.state)

    def test_legacy_corruption_is_still_rejected_when_journal_exists(self):
        self.legacy({self.address: self.observation()})
        self.journal()
        path = self.case / "address-counts.json"
        data = read_json(path)
        data["counts"][self.address]["confirmed_tx_count"] = 99
        save_json(path, data)
        with self.assertRaisesRegex(TraceError, "Invalid address count cache"):
            _cache(self.case, self.state)

    def test_storage_and_sidecar_symlinks_are_rejected(self):
        destination = self.case / "unrelated"
        destination.write_text("do not change")
        for suffix in ("", "-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                path = self.case / ("address-counts.sqlite3" + suffix)
                path.symlink_to(destination)
                try:
                    with self.assertRaisesRegex(TraceError, "symbolic link"):
                        _cache(self.case, self.state)
                    with self.assertRaisesRegex(TraceError, "symbolic link"):
                        CountCacheJournal(self.case, self.state["case_id"], SOURCE)
                finally:
                    path.unlink()
        self.assertEqual(destination.read_text(), "do not change")


if __name__ == "__main__":
    unittest.main()
