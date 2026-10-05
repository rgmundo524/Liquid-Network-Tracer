"""Synthetic archives exercise cold verification and authenticated warm reads."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from liquid_tracer.cli import verify_export
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.snapshot_index import open_snapshot_index, verified_source_identity
from tests.test_attribution_convergence import graph_state, tx
from tests.test_pegout_paths import add_pegout


class SnapshotIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dataset = Path(temporary.name) / "dataset"
        self.archive = self.dataset / "runs" / ("a" * 16)
        self.archive.mkdir(parents=True)
        self.state = graph_state((("a:0", "b"), ("b:0", "c"), ("a:1", "d")), seeds=("a:0", "a:1"),
                                 raw_links=(("b:1", "e"),))
        add_pegout(self.state, tx("c"))
        self.state.update(case_id="b" * 32, run_id="a" * 16, source="fixture:synthetic",
                          shared_collection={"dataset_id": "b" * 32},
                          address_tx_counts={"SYNTHETIC-a-address": {"tx_count": 4}})
        rows = []
        for oid, (txid, record) in enumerate(self.state["transactions"].items(), 1):
            record["observation_id"] = oid
            raw = canonical(record["data"])
            name = f"evidence/{oid:08d}.response"
            (self.archive / name).parent.mkdir(exist_ok=True)
            (self.archive / name).write_bytes(raw)
            rows.append({"id": oid, "endpoint": "/tx/" + txid, "source": self.state["source"],
                         "status": 200, "file": name, "sha256": digest(raw)})
        self.state["observations"] = [row["id"] for row in rows]
        save_json(self.archive / "evidence-index.json", rows)
        save_json(self.archive / "graph.json", {})
        save_json(self.archive / "miro-plan.json", {})
        self.seal()

    def seal(self):
        save_json(self.archive / "trace.json", self.state)
        manifest = self.archive / "SHA256SUMS"
        files = sorted(path for path in self.archive.rglob("*") if path.is_file() and path != manifest)
        manifest.write_text("".join(digest(path.read_bytes()) + "  " + str(path.relative_to(self.archive)) + "\n"
                                    for path in files))

    def open(self, **kwargs):
        return open_snapshot_index(self.archive, case_id=self.state["case_id"], run_id=self.state["run_id"],
                                   source=self.state["source"], **kwargs)

    def test_cold_and_warm_index_preserve_records_and_recover_exact_saved_spends(self):
        before = {str(path.relative_to(self.archive)): path.read_bytes()
                  for path in self.archive.rglob("*") if path.is_file()}
        with patch("liquid_tracer.cli.verify_export", wraps=verify_export) as verify:
            with self.open() as index:
                self.assertEqual(verify.call_count, 1)
                self.assertEqual(index.transaction(tx("a")), self.state["transactions"][tx("a")])
                self.assertIsNone(index.transaction(tx("f")))
                self.assertEqual(index.output(tx("a") + ":0"), self.state["outputs"][tx("a") + ":0"])
                self.assertEqual(index.link(tx("b") + ":1")["spending_txid"], tx("e"))
                self.assertEqual(index.link(tx("b") + ":1")["relationship"], "saved_transaction_input")
                self.assertEqual({row["spending_txid"] for row in index.outgoing(tx("a"))}, {tx("b"), tx("d")})
                self.assertEqual([row["outpoint"] for row in index.incoming(tx("c"))], [tx("b") + ":0"])
                self.assertEqual(index.counts(["unknown", "SYNTHETIC-a-address"]), self.state["address_tx_counts"])
                self.assertEqual(index.observation(1), read_json(self.archive / "evidence-index.json")[0])
                self.assertIsNone(index.observation(999))
                self.assertTrue(index.path.is_relative_to(self.dataset / "indexes"))
                self.assertNotIn("transactions", index.metadata)
                self.assertEqual(index.metadata["_snapshot_collected_depth"], 0)
        original_read = read_json
        def read_small(path):
            self.assertNotEqual(Path(path).name, "trace.json")
            return original_read(path)
        with patch("liquid_tracer.cli.verify_export", side_effect=AssertionError("full verify on warm read")), \
             patch("liquid_tracer.snapshot_index.read_json", side_effect=read_small):
            with self.open() as index:
                self.assertEqual(index.transaction(tx("c")), self.state["transactions"][tx("c")])
                self.assertIsNone(index.link(tx("c") + ":0"))
        after = {str(path.relative_to(self.archive)): path.read_bytes()
                 for path in self.archive.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_changed_archive_is_reverified_and_fails_on_corruption(self):
        with self.open():
            pass
        path = self.archive / "trace.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with patch("liquid_tracer.cli.verify_export", wraps=verify_export) as verify:
            with self.assertRaisesRegex(TraceError, "checksum mismatch"):
                with self.open():
                    self.fail("Corrupted archive accepted")
            self.assertEqual(verify.call_count, 1)

    def test_resealed_change_builds_a_new_revision(self):
        with self.open() as index:
            original_path = index.path
        self.state["stop_reason"] = "different revision"
        self.seal()
        with self.open() as index:
            self.assertNotEqual(index.path, original_path)
            self.assertEqual(index.metadata["stop_reason"], "different revision")

    def test_corrupt_database_or_catalog_rebuilds_with_full_verification(self):
        for part in ("database", "catalog"):
            with self.subTest(part=part):
                with self.open() as index:
                    target = index.path if part == "database" else index.path.with_suffix(".json")
                target.write_bytes(b"broken")
                with patch("liquid_tracer.cli.verify_export", wraps=verify_export) as verify:
                    with self.open() as index:
                        self.assertIsNotNone(index.link(tx("a") + ":0"))
                    self.assertEqual(verify.call_count, 1)

    def test_changed_catalog_metadata_is_not_trusted(self):
        with self.open() as index:
            catalog_path = index.path.with_suffix(".json")
        envelope = read_json(catalog_path)
        envelope["payload"]["metadata"]["source"] = "changed-source"
        save_json(catalog_path, envelope)
        with patch("liquid_tracer.cli.verify_export", wraps=verify_export) as verify:
            with self.open() as index:
                self.assertEqual(index.metadata["source"], self.state["source"])
            self.assertEqual(verify.call_count, 1)

    def test_missing_link_never_becomes_an_unspent_endpoint(self):
        # Simulate silent media damage not reflected by a file-stat change.
        from liquid_tracer import snapshot_index
        with self.open() as index:
            expected_stat = index._catalog["database_stat"]
            real_stat = snapshot_index._stat
            with sqlite3.connect(index.path) as database:
                database.execute("DELETE FROM records WHERE kind='link' AND key=?", (tx("a") + ":0",))
            with patch("liquid_tracer.snapshot_index._stat", side_effect=lambda path:
                       expected_stat if Path(path) == index.path else real_stat(path)):
                with self.assertRaisesRegex(TraceError, "missing or corrupt"):
                    index.link(tx("a") + ":0")

    def test_corrupt_payload_never_returns_unverified_data(self):
        from liquid_tracer import snapshot_index
        with self.open() as index:
            expected_stat = index._catalog["database_stat"]
            real_stat = snapshot_index._stat
            with sqlite3.connect(index.path) as database:
                database.execute("UPDATE records SET payload=? WHERE kind='transaction' AND key=?",
                                 (b"{}", tx("a")))
            with patch("liquid_tracer.snapshot_index._stat", side_effect=lambda path:
                       expected_stat if Path(path) == index.path else real_stat(path)):
                with self.assertRaisesRegex(TraceError, "missing or corrupt"):
                    index.transaction(tx("a"))

    def test_stat_change_during_open_index_fails_closed(self):
        with self.open() as index:
            index.transaction(tx("a"))
            with index.path.open("ab") as stream:
                stream.write(b"changed")
            with self.assertRaisesRegex(TraceError, "changed during use"):
                index.transaction(tx("a"))

    def test_symlinked_archive_member_and_index_are_rejected(self):
        member = self.archive / "graph.json"
        moved = self.dataset / "external.json"
        member.replace(moved)
        member.symlink_to(moved)
        with self.assertRaisesRegex(TraceError, "symbolic links"):
            with self.open():
                pass
        member.unlink()
        moved.replace(member)
        with self.open() as index:
            path = index.path
        external = self.dataset / "external.sqlite"
        path.replace(external)
        path.symlink_to(external)
        with self.assertRaisesRegex(TraceError, "symbolic links"):
            with self.open():
                pass

    def test_identity_and_global_evidence_validation_precede_queries(self):
        with self.assertRaisesRegex(TraceError, "identity"):
            with open_snapshot_index(self.archive, case_id="c" * 32, run_id=self.state["run_id"], source=self.state["source"]):
                pass
        # Corruption on a disconnected/unselected branch must still fail cold construction.
        self.state["transactions"][tx("d")]["data"]["vout"][0]["pegout"] = {"genesis_hash": "bad"}
        self.seal()
        with self.assertRaisesRegex(TraceError, "peg-out"):
            with self.open():
                pass

    def test_private_archive_source_lookup_supports_optional_source(self):
        self.state.pop("shared_collection")
        self.seal()
        with open_snapshot_index(self.archive, case_id=self.state["case_id"], run_id=self.state["run_id"]) as index:
            self.assertEqual(index.metadata["source"], self.state["source"])

    def test_concurrent_openers_share_one_builder(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def blocked_verify(*args, **kwargs):
            calls.append(1)
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release builder")
            return verify_export(*args, **kwargs)
        def load():
            with self.open() as index:
                return index.transaction(tx("a"))
        with patch("liquid_tracer.cli.verify_export", side_effect=blocked_verify):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(load)
                self.assertTrue(entered.wait(5))
                second = pool.submit(load)
                release.set()
                self.assertEqual(first.result(timeout=5), second.result(timeout=5))
        self.assertEqual(len(calls), 1)

    def legacy_private(self):
        self.state = {key: self.state[key] for key in ("case_id", "run_id", "source")}
        (self.archive / "evidence-index.json").unlink()
        self.seal()

    def source_identity(self, **kwargs):
        return verified_source_identity(self.archive, case_id=self.state["case_id"],
                                        run_id=self.state["run_id"], **kwargs)

    def test_legacy_private_source_identity_needs_no_graph_or_observation_index(self):
        self.legacy_private()
        with patch("liquid_tracer.cli.verify_export", wraps=verify_export) as verify:
            self.assertEqual(self.source_identity(), {**self.state, "blockchain": "liquid"})
            self.assertEqual(verify.call_count, 1)
        self.assertEqual(list((self.dataset / "indexes").glob("*.sqlite")), [])
        original_read = read_json
        def read_small(path):
            self.assertNotEqual(Path(path).name, "trace.json")
            return original_read(path)
        with patch("liquid_tracer.cli.verify_export", side_effect=AssertionError("warm full read")), \
             patch("liquid_tracer.snapshot_index.read_json", side_effect=read_small):
            self.assertEqual(self.source_identity(), {**self.state, "blockchain": "liquid"})

    def test_private_source_identity_caches_explicit_bitcoin_without_rewriting_evidence(self):
        self.legacy_private()
        self.state["blockchain"] = "bitcoin"
        self.seal()
        evidence = (self.archive / "trace.json").read_bytes()
        with patch("liquid_tracer.cli.verify_export", wraps=verify_export) as verify:
            self.assertEqual(self.source_identity(), self.state)
            self.assertEqual(self.source_identity(), self.state)
            self.assertEqual(verify.call_count, 1)
        self.assertEqual((self.archive / "trace.json").read_bytes(), evidence)

    def test_private_identity_cache_corruption_reverifies_source(self):
        self.legacy_private()
        self.source_identity()
        cache = next((self.dataset / "indexes").glob("source-*.json"))
        envelope = read_json(cache)
        envelope["payload"]["identity"]["source"] = "corrupt"
        save_json(cache, envelope)
        with patch("liquid_tracer.cli.verify_export", wraps=verify_export) as verify:
            self.assertEqual(self.source_identity(), {**self.state, "blockchain": "liquid"})
            self.assertEqual(verify.call_count, 1)

    def test_private_identity_changed_archive_and_wrong_identity_fail_closed(self):
        self.legacy_private()
        self.source_identity()
        with self.assertRaisesRegex(TraceError, "identity"):
            verified_source_identity(self.archive, case_id="wrong", run_id=self.state["run_id"])
        with (self.archive / "trace.json").open("ab") as stream:
            stream.write(b" ")
        with self.assertRaisesRegex(TraceError, "checksum mismatch"):
            self.source_identity()

    def test_private_identity_concurrent_openers_share_one_verification(self):
        self.legacy_private()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def blocked_verify(*args, **kwargs):
            calls.append(1)
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release verifier")
            return verify_export(*args, **kwargs)
        with patch("liquid_tracer.cli.verify_export", side_effect=blocked_verify):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.source_identity)
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.source_identity)
                release.set()
                self.assertEqual(first.result(timeout=5), second.result(timeout=5))
        self.assertEqual(calls, [1])

    def test_cancellation_during_build_leaves_no_published_index(self):
        def cancel(event):
            if event["phase"] == "indexing_collection":
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            with self.open(progress=cancel):
                pass
        self.assertEqual(list((self.dataset / "indexes").glob("*.sqlite")), [])
        self.assertEqual(list((self.dataset / "indexes").glob(".snapshot-*")), [])
        with self.open() as index:
            self.assertIsNotNone(index.transaction(tx("a")))

    def test_cancellation_while_waiting_for_builder(self):
        with self.open() as index:
            lock_path = index.path.with_suffix(".lock")
        def cancel(event):
            if event["phase"] == "waiting_collection_index":
                raise KeyboardInterrupt
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with self.assertRaises(KeyboardInterrupt):
                    with self.open(progress=cancel):
                        pass
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
