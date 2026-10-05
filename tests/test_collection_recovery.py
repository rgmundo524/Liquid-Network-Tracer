"""Finished traces interrupted during statistics can be sealed offline safely."""
import contextlib
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.address_counts import addresses
from liquid_tracer.api import Esplora
from liquid_tracer.cli import main, verify_export
from liquid_tracer.collection_recovery import recover_collection
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.shared_collection import collect_prepared, dataset_path, load_shared_run, prepare_collection
from liquid_tracer.store import Store
from tests.fixtures import A, B, fixture


class CollectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = self.root / "fixture.json"
        data = fixture()
        wanted = {output.get("scriptpubkey_address") for endpoint, transaction in data.items()
                  if not endpoint.endswith("/outspends")
                  for output in [*transaction["vout"], *(item.get("prevout") or {} for item in transaction["vin"]) ]}
        for address in wanted - {None}:
            data["/address/" + address] = {"address": address, "chain_stats": {"tx_count": 7}, "mempool_stats": {"tx_count": 1}}
        save_json(self.fixture, data)
        self.case = create_investigation(self.root, "Interrupted counts", seeds=[A + ":0"], fixture=self.fixture)
        prepared = prepare_collection(self.case, [read_case(self.case)["case_id"]], hops=1)
        self.dataset = dataset_path(self.case)

        def interrupt_counts(case, state, **kwargs):
            self.saved_state = copy.deepcopy(state)
            self.run_id = state["run_id"]
            self.known = addresses(state)[0]
            record = {"address": self.known, "source": state["source"], "observed_at": "2026-01-01T00:00:00+00:00",
                      "confirmed_tx_count": 7, "mempool_tx_count": 1}
            cache = {"schema_version": 1, "case_id": state["case_id"], "source": state["source"], "counts": {self.known: record}}
            cache["sha256"] = digest(canonical(cache))
            save_json(case / "address-counts.json", cache)
            raise KeyboardInterrupt

        with patch("liquid_tracer.cli.ensure_counts", side_effect=interrupt_counts):
            with self.assertRaises(KeyboardInterrupt):
                collect_prepared(self.case, prepared["request_id"])
        self.archive = self.dataset / "runs" / self.run_id
        self.original = (self.archive / "trace.json").read_bytes()
        self.assertFalse((self.archive / "SHA256SUMS").exists())
        self.assertIsNone(read_case(self.dataset).get("latest_run"))

    def test_offline_recovery_retains_trace_counts_and_original_checkpoint(self):
        with patch.object(Esplora, "__init__", side_effect=AssertionError("Recovery must stay offline")):
            report = recover_collection(self.dataset)
        verify_export(self.archive)
        _, recovered, _ = load_shared_run(self.case, self.run_id)
        self.assertEqual(recovered["transactions"], self.saved_state["transactions"])
        self.assertEqual(recovered["outputs"], self.saved_state["outputs"])
        self.assertEqual(recovered["links"], self.saved_state["links"])
        self.assertEqual(recovered["status"], self.saved_state["status"])
        self.assertEqual(recovered["limits"], self.saved_state["limits"])
        self.assertEqual(recovered["shared_collection"], self.saved_state["shared_collection"])
        self.assertEqual((self.archive / "recovery-original-trace.json").read_bytes(), self.original)
        self.assertEqual(report["original_checkpoint_sha256"], digest(self.original))
        self.assertEqual(report["address_counts"]["known"], 1)
        self.assertGreater(report["address_counts"]["remaining"], 0)
        self.assertFalse(report["address_counts"]["complete"])
        self.assertEqual(recovered["address_tx_counts"][self.known]["confirmed_tx_count"], 7)
        self.assertEqual(read_case(self.dataset)["latest_run"], self.run_id)

    def test_zero_hop_continue_only_fetches_missing_counts_and_preserves_limit(self):
        recover_collection(self.dataset, self.run_id)
        prepared = prepare_collection(self.case, hops=0, resume=self.run_id)
        original_get, calls = Esplora._get, []

        def tracked_get(api, endpoint):
            calls.append(endpoint)
            return original_get(api, endpoint)

        with patch.object(Esplora, "_get", tracked_get), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collect_prepared(self.case, prepared["request_id"]), 0)
        _, state, _ = load_shared_run(self.case)
        self.assertEqual(state["limits"]["max_hops"], 1)
        self.assertNotIn("collection_recovery", state)
        self.assertEqual(set(state["transactions"]), set(self.saved_state["transactions"]))
        self.assertEqual(calls, ["/address/" + address for address in addresses(state) if address != self.known])

    def test_cli_supports_explicit_run_without_unlocking_credentials(self):
        output = io.StringIO()
        with patch.object(Esplora, "__init__", side_effect=AssertionError("No API client")), contextlib.redirect_stdout(output):
            result = main(["recover-collection", "--case", str(self.dataset), "--run", self.run_id])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["run_id"], self.run_id)

    def test_private_collection_recovery_uses_the_same_offline_validation(self):
        private = create_investigation(self.root, "Private recovery", seeds=[A + ":0"], fixture=self.fixture)
        with patch("liquid_tracer.cli.ensure_counts", side_effect=KeyboardInterrupt), contextlib.redirect_stderr(io.StringIO()):
            result = main(["trace", "--case", str(private), "--seed", A + ":0", "--fixture", str(self.fixture), "--hops", "1"])
        self.assertEqual(result, 130)
        with patch.object(Esplora, "__init__", side_effect=AssertionError("Offline only")):
            report = recover_collection(private)
        self.assertFalse(report["shared_collection"])
        verify_export(private / "runs" / report["run_id"])
        self.assertEqual(read_case(private)["latest_run"], report["run_id"])

    def test_active_trace_and_count_locks_reject_recovery(self):
        for name in ("trace.lock", "address-counts.lock"):
            with self.subTest(name=name), (self.dataset / name).open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(TraceError, "still active"):
                    recover_collection(self.dataset)
            self.assertEqual((self.archive / "trace.json").read_bytes(), self.original)

    def test_sealed_run_is_never_overwritten(self):
        recover_collection(self.dataset)
        before = (self.archive / "SHA256SUMS").read_bytes()
        with self.assertRaisesRegex(TraceError, "already sealed"):
            recover_collection(self.dataset, self.run_id)
        self.assertEqual((self.archive / "SHA256SUMS").read_bytes(), before)

    def test_validation_rejects_tampered_checkpoint_before_writing(self):
        mutations = {
            "running": lambda state: state.update(status="running", finished_at=None),
            "identity": lambda state: state.update(case_id="f" * 32),
            "source": lambda state: state.update(source="https://different.invalid"),
            "transaction": lambda state: state["transactions"][A]["data"].update(fee=999),
            "spend": lambda state: state["outputs"][A + ":0"]["observed_spend"].update(vin=99),
            "bookkeeping": lambda state: state["stats"].update(outputs_cumulative=999),
            "frozen_members": lambda state: state["shared_collection"].update(members=[]),
        }
        for label, change in mutations.items():
            with self.subTest(label=label):
                state = json.loads(self.original)
                change(state)
                save_json(self.archive / "trace.json", state)
                before = (self.archive / "trace.json").read_bytes()
                with self.assertRaises(TraceError):
                    recover_collection(self.dataset, self.run_id)
                self.assertFalse((self.archive / "SHA256SUMS").exists())
                self.assertEqual((self.archive / "trace.json").read_bytes(), before)
                self.assertIsNone(read_case(self.dataset).get("latest_run"))

    def test_evidence_corruption_rejected(self):
        with sqlite3.connect(self.dataset / "evidence.sqlite") as connection:
            connection.execute("UPDATE observations SET body=? WHERE endpoint=?", (b"{}", "/tx/" + A))
        with self.assertRaisesRegex(TraceError, "altered.*evidence"):
            recover_collection(self.dataset)
        self.assertEqual((self.archive / "trace.json").read_bytes(), self.original)

    def test_retry_response_history_is_retained_but_not_accepted_as_transaction(self):
        state = json.loads(self.original)
        store = Store(self.dataset)
        try:
            retry = store.observe(self.run_id, state["source"], "/tx/" + A,
                                  b'{"error":"temporary unavailable"}', status=503)
        finally:
            store.close()
        state["observations"].append(retry)
        save_json(self.archive / "trace.json", state)
        recover_collection(self.dataset)
        recovered = read_json(self.archive / "trace.json")
        self.assertIn(retry, recovered["observations"])
        self.assertEqual(next(row for row in read_json(self.archive / "evidence-index.json")
                              if row["id"] == retry)["status"], 503)

    def test_changed_latest_pointer_rejected(self):
        metadata = read_case(self.dataset)
        metadata["latest_run"] = "a" * 16
        save_json(self.dataset / "case.json", metadata)
        with self.assertRaisesRegex(TraceError, "advanced"):
            recover_collection(self.dataset)
        self.assertEqual(read_case(self.dataset)["latest_run"], "a" * 16)

    def test_ambiguous_unsealed_runs_require_explicit_selection(self):
        duplicate = self.dataset / "runs" / ("b" * 16)
        duplicate.mkdir()
        (duplicate / "trace.json").write_bytes(self.original)
        with self.assertRaisesRegex(TraceError, "Choose --run"):
            recover_collection(self.dataset)

    def test_export_failure_retains_original_checkpoint_and_pointer(self):
        with patch("liquid_tracer.export.export_run", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaises(OSError):
                recover_collection(self.dataset)
        self.assertEqual((self.archive / "trace.json").read_bytes(), self.original)
        self.assertIsNone(read_case(self.dataset).get("latest_run"))
        self.assertFalse(list(self.dataset.glob(".collection-recovery-*")))

    def test_partial_install_retains_original_and_can_be_retried(self):
        replace = os.replace

        def fail_manifest(source, destination):
            if Path(source).name == "SHA256SUMS":
                raise OSError("Synthetic final manifest failure")
            return replace(source, destination)

        with patch("liquid_tracer.collection_recovery.os.replace", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                recover_collection(self.dataset)
        self.assertFalse((self.archive / "SHA256SUMS").exists())
        self.assertIsNone(read_case(self.dataset).get("latest_run"))
        self.assertEqual((self.archive / "recovery-original-trace.json").read_bytes(), self.original)
        recover_collection(self.dataset)
        verify_export(self.archive)
        self.assertEqual((self.archive / "recovery-original-trace.json").read_bytes(), self.original)

    def test_latest_change_during_staging_keeps_original_archive_unsealed(self):
        from liquid_tracer.export import export_run

        def changed_latest(*args, **kwargs):
            result = export_run(*args, **kwargs)
            metadata = read_case(self.dataset)
            metadata["latest_run"] = "c" * 16
            save_json(self.dataset / "case.json", metadata)
            return result

        with patch("liquid_tracer.export.export_run", side_effect=changed_latest):
            with self.assertRaisesRegex(TraceError, "advanced during recovery"):
                recover_collection(self.dataset)
        self.assertEqual((self.archive / "trace.json").read_bytes(), self.original)
        self.assertFalse((self.archive / "SHA256SUMS").exists())
        self.assertEqual(read_case(self.dataset)["latest_run"], "c" * 16)

    def interrupted_latest_save(self):
        def fail_latest(path, value):
            if Path(path) == self.dataset / "case.json":
                raise OSError("Synthetic latest-pointer disk failure")
            return save_json(path, value)

        with patch("liquid_tracer.collection_recovery.save_json", side_effect=fail_latest):
            with self.assertRaises(OSError):
                recover_collection(self.dataset)
        verify_export(self.archive)
        self.assertIsNone(read_case(self.dataset).get("latest_run"))
        return {str(path.relative_to(self.archive)): path.read_bytes()
                for path in self.archive.rglob("*") if path.is_file()}

    def test_completed_archive_with_failed_latest_save_finishes_without_rewrite(self):
        before = self.interrupted_latest_save()
        with patch("liquid_tracer.export.export_run", side_effect=AssertionError("Do not rewrite sealed archive")):
            report = recover_collection(self.dataset)
        self.assertEqual(report["run_id"], self.run_id)
        self.assertEqual(read_case(self.dataset)["latest_run"], self.run_id)
        self.assertEqual(before, {str(path.relative_to(self.archive)): path.read_bytes()
                                  for path in self.archive.rglob("*") if path.is_file()})
        load_shared_run(self.case)

    def test_sealed_pending_recovery_rejects_changed_latest(self):
        before = self.interrupted_latest_save()
        metadata = read_case(self.dataset)
        metadata["latest_run"] = "c" * 16
        save_json(self.dataset / "case.json", metadata)
        with self.assertRaisesRegex(TraceError, "advanced"):
            recover_collection(self.dataset, self.run_id)
        self.assertEqual(read_case(self.dataset)["latest_run"], "c" * 16)
        self.assertEqual(before, {str(path.relative_to(self.archive)): path.read_bytes()
                                  for path in self.archive.rglob("*") if path.is_file()})

    def test_interrupted_expansion_can_be_recovered_against_sealed_parent(self):
        recover_collection(self.dataset)
        prepared = prepare_collection(self.case, hops=1, resume=self.run_id)
        with patch("liquid_tracer.cli.ensure_counts", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                collect_prepared(self.case, prepared["request_id"])
        with patch.object(Esplora, "__init__", side_effect=AssertionError("Offline only")):
            report = recover_collection(self.dataset)
        _, state, _ = load_shared_run(self.case, report["run_id"])
        self.assertEqual(state["parent_run"], self.run_id)
        self.assertEqual(state["limits"]["max_hops"], 2)
        self.assertEqual(state["shared_collection"]["members"], self.saved_state["shared_collection"]["members"])

    def test_symlink_rejected(self):
        link = self.root / "linked-case"
        link.symlink_to(self.dataset, target_is_directory=True)
        with self.assertRaisesRegex(TraceError, "symbolic"):
            recover_collection(link)


if __name__ == "__main__":
    unittest.main()
