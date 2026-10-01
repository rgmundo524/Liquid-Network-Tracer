"""Shared collection freezes its membership and one explicit case policy."""
import contextlib
import fcntl
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.api import Esplora
from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import create_investigation, list_investigations, read_case, update_case
from liquid_tracer.services import set_service
from liquid_tracer.shared_collection import (collect_prepared, dataset_path, load_shared_run,
                                              prepare_collection, read_summary)
from tests.fixtures import A, B, C, D, fixture
from tests.test_trace_concurrency import evidence_topology


class SharedCollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = self.root / "fixture.json"
        save_json(self.fixture, fixture())
        self.settings = {"hops": 1, "budget_limits_enabled": True, "max_transactions": 100, "max_outpoints": 500,
                         "max_requests": 500, "max_seconds": 30}
        self.first = create_investigation(self.root, "First", seeds=[A + ":0"],
                                         fixture=self.fixture, run_defaults=self.settings)
        self.second = create_investigation(self.root, "Second", seeds=[B + ":1"],
                                          fixture=self.fixture, run_defaults=self.settings)
        self.members = [read_case(case)["case_id"] for case in (self.first, self.second)]

    def collect(self, prepared):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(collect_prepared(self.first, prepared["request_id"]), 0)
        return json.loads(output.getvalue())

    def prepare(self, hops=1, members=None, **kwargs):
        return prepare_collection(self.first, self.members if members is None else members, hops=hops, **kwargs)

    def test_read_summary_does_not_create_storage_and_pool_stays_out_of_catalog(self):
        self.assertEqual(read_summary(self.root)["runs"], [])
        self.assertFalse(dataset_path(self.first).exists())
        prepared = self.prepare()
        self.assertEqual(prepared["seeds"], sorted([A + ":0", B + ":1"]))
        self.assertEqual({path for path, _ in list_investigations(self.root)}, {self.first, self.second})

    def test_summary_reuses_small_cached_metadata_without_reverifying_archives(self):
        self.collect(self.prepare())
        expected = read_summary(self.root, case=self.first)
        with patch("liquid_tracer.cli.verify_export", side_effect=AssertionError("Public summary must not hash archives")):
            self.assertEqual(read_summary(self.root, case=self.first), expected)

    def test_collect_uses_union_seeds_once_and_preserves_private_investigations(self):
        before = {case: (case / "case.json").read_bytes() for case in (self.first, self.second)}
        result = self.collect(self.prepare())
        path, state, archive = load_shared_run(self.second, result["run_id"], dataset_id=result["dataset_id"])
        self.assertEqual(state["seeds"], sorted([A + ":0", B + ":1"]))
        self.assertEqual(set(state["transactions"]), {A, B, C})
        self.assertEqual(state["shared_collection"]["policy_case_name"], "First")
        self.assertEqual(state["shared_collection"]["members"][1]["name"], "Second")
        index = read_json(archive / "evidence-index.json")
        self.assertEqual(len({row["endpoint"] for row in index}), len(index))
        for case in (self.first, self.second):
            self.assertEqual((case / "case.json").read_bytes(), before[case])
            self.assertFalse((case / "evidence.sqlite").exists())
        summary = read_summary(self.root, case=self.second)
        self.assertTrue(summary["compatible"])
        self.assertEqual(summary["latest_run"], result["run_id"])
        self.assertEqual(summary["runs"][0]["id"], result["run_id"])
        self.assertEqual(summary["runs"][0]["transaction_count"], 3)
        self.assertNotIn("source", summary)
        self.assertNotIn(str(self.root), json.dumps(summary))

    def test_continue_keeps_members_and_seeds_while_fresh_collect_replaces_only_latest(self):
        initial = self.collect(self.prepare())
        _, state, archive = load_shared_run(self.first, initial["run_id"])
        original = {str(path.relative_to(archive)): path.read_bytes() for path in archive.rglob("*") if path.is_file()}
        later = create_investigation(self.root, "Later tab", seeds=[D + ":0"], fixture=self.fixture)
        prepared = prepare_collection(self.first, None, hops=1, resume=initial["run_id"])
        continued = self.collect(prepared)
        _, next_state, _ = load_shared_run(self.first, continued["run_id"])
        self.assertEqual(next_state["seeds"], state["seeds"])
        self.assertEqual(next_state["shared_collection"]["members"], state["shared_collection"]["members"])
        self.assertEqual(next_state["limits"]["max_hops"], 2)
        self.assertEqual(next_state["parent_run"], initial["run_id"])
        self.assertIn(D, next_state["transactions"])
        fresh = self.collect(self.prepare(hops=0, members=[read_case(later)["case_id"]]))
        _, fresh_state, _ = load_shared_run(self.first, fresh["run_id"])
        self.assertEqual(fresh_state["seeds"], [D + ":0"])
        self.assertIsNone(fresh_state["parent_run"])
        self.assertEqual(original, {str(path.relative_to(archive)): path.read_bytes() for path in archive.rglob("*") if path.is_file()})

    def test_failed_collection_is_saved_and_zero_hop_continuation_recovers_all_paths(self):
        update_case(self.first, {"run_defaults": {**self.settings, "budget_limits_enabled": False}})
        prepared = self.prepare(hops=15)
        original_get = Esplora._get

        def fail_outspends(api, endpoint):
            if endpoint == "/tx/" + B + "/outspends":
                raise TraceError("Network request failed: RemoteDisconnected")
            return original_get(api, endpoint)

        output = io.StringIO()
        with patch.object(Esplora, "_get", fail_outspends), contextlib.redirect_stdout(output):
            self.assertEqual(collect_prepared(self.first, prepared["request_id"]), 1)
        failed = json.loads(output.getvalue())
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["errors"], ["Network request failed: RemoteDisconnected"])
        self.assertEqual(read_case(dataset_path(self.first))["latest_run"], failed["run_id"])
        _, state, archive = load_shared_run(self.first, failed["run_id"])
        original = {str(path.relative_to(archive)): path.read_bytes()
                    for path in archive.rglob("*") if path.is_file()}
        self.assertGreater(len(state["transactions"]), 0)
        self.assertIn(A + ":0", state["links"])
        self.assertGreater(state["stats"]["frontier_count"], 0)
        self.assertFalse(any(item["status"] == "pending" for item in state["outputs"].values()))
        summary = read_summary(self.root, case=self.first)
        self.assertEqual(summary["latest_run"], failed["run_id"])
        self.assertEqual(summary["latest"]["status"], "error")

        recovery = prepare_collection(self.first, None, hops=0, resume=failed["run_id"])
        self.assertEqual(recovery["policy"]["settings"]["hops"], 15)
        result = self.collect(recovery)
        _, recovered, _ = load_shared_run(self.first, result["run_id"])
        self.assertEqual(recovered["limits"]["max_hops"], 15)
        self.assertEqual(recovered["parent_run"], failed["run_id"])
        self.assertEqual(recovered["status"], "bounded_complete")
        self.assertEqual(recovered["errors"], [])
        self.assertTrue(set(state["transactions"]).issubset(recovered["transactions"]))
        self.assertTrue(set(state["outputs"]).issubset(recovered["outputs"]))
        self.assertTrue(set(state["observations"]).issubset(recovered["observations"]))
        self.assertEqual({key: recovered["links"][key] for key in state["links"]}, state["links"])
        self.assertEqual(recovered["shared_collection"]["members"], state["shared_collection"]["members"])
        self.assertEqual(original, {str(path.relative_to(archive)): path.read_bytes()
                                    for path in archive.rglob("*") if path.is_file()})

        complete = self.collect(self.prepare(hops=15))
        _, uninterrupted, _ = load_shared_run(self.first, complete["run_id"])
        self.assertEqual(evidence_topology(recovered), evidence_topology(uninterrupted))

    def test_policy_and_membership_are_frozen_before_credential_delay(self):
        set_service(self.first, "SYNTHETIC-victim-deposit", name="Policy stop", stop_tracing=True)
        prepared = self.prepare(hops=3, members=[self.members[0]])
        set_service(self.first, "SYNTHETIC-victim-deposit", name="Later cleared", stop_tracing=False)
        update_case(self.first, {"run_defaults": {**self.settings, "max_transactions": 1}})
        result = self.collect(prepared)
        _, state, _ = load_shared_run(self.first, result["run_id"])
        self.assertEqual(set(state["transactions"]), {A})
        self.assertTrue(any(label.get("stop") for label in state["labels"]))
        self.assertEqual(state["limits"]["max_transactions"], 100)
        self.assertEqual(state["shared_collection"]["settings"]["hops"], 3)

    def test_address_counts_use_the_frozen_shared_policy_not_dataset_defaults(self):
        prepared = self.prepare(hops=0)
        dataset = dataset_path(self.first)
        self.assertFalse(read_case(dataset)["run_defaults"]["budget_limits_enabled"])
        with patch("liquid_tracer.address_counts._collect_counts", return_value={}) as counts:
            self.collect(prepared)
        self.assertEqual(counts.call_args.kwargs["max_requests"], 500)
        self.assertEqual(counts.call_args.kwargs["max_seconds"], 30)

    def test_other_members_stop_rules_are_not_merged(self):
        set_service(self.second, "SYNTHETIC-victim-deposit", name="Other stop", stop_tracing=True)
        result = self.collect(self.prepare(hops=2))
        _, state, _ = load_shared_run(self.first, result["run_id"])
        self.assertIn(C, state["transactions"])
        self.assertIn(A + ":0", state["links"])
        self.assertFalse(any(label.get("stop") for label in state["labels"]))

    def test_source_mismatch_and_changed_fixture_fail_before_requests(self):
        different = self.root / "different.json"
        save_json(different, {**fixture(), "/synthetic": {}})
        outsider = create_investigation(self.root, "Different source", seeds=[A + ":0"], fixture=different)
        self.prepare()
        with self.assertRaisesRegex(TraceError, "same blockchain and API source"):
            self.prepare(members=[read_case(outsider)["case_id"]])
        summary = read_summary(self.root, case=outsider)
        self.assertFalse(summary["compatible"])
        self.assertNotIn(str(self.root), json.dumps(summary))
        prepared = self.prepare()
        save_json(self.fixture, {**fixture(), "/changed": {}})
        with self.assertRaisesRegex(TraceError, "fixture changed"):
            self.collect(prepared)

    def test_stale_prepared_continuation_cannot_move_latest_backwards(self):
        first = self.collect(self.prepare())
        stale = prepare_collection(self.first, None, hops=1, resume=first["run_id"])
        next_request = prepare_collection(self.first, None, hops=2, resume=first["run_id"])
        next_run = self.collect(next_request)
        with self.assertRaisesRegex(TraceError, "advanced"):
            self.collect(stale)
        self.assertEqual(read_case(dataset_path(self.first))["latest_run"], next_run["run_id"])
        with self.assertRaisesRegex(TraceError, "advanced"):
            prepare_collection(self.first, None, hops=1, resume=first["run_id"])

    def test_shared_lock_blocks_other_shared_writers_but_not_private_collection(self):
        prepared = self.prepare()
        path = dataset_path(self.first)
        with (path / "trace.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TraceError, "Another trace"):
                self.collect(prepared)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = main(["trace", "--case", str(self.first), "--seed", A + ":0", "--hops", "0",
                               "--fixture", str(self.fixture)])
            self.assertEqual(status, 0)
            self.assertTrue(read_case(self.first).get("latest_run"))

    def test_cli_prepared_request_and_invalid_controls(self):
        prepared = self.prepare(hops=0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(["shared-collect", "--case", str(self.first), "--request", prepared["request_id"]])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["shared_collection"])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["trace", "--case", str(dataset_path(self.first)), "--seed", A + ":0",
                                   "--hops", "0", "--fixture", str(self.fixture)]), 1)
        for invalid in ([], ["../outside"], [self.members[0], self.members[0]]):
            with self.subTest(invalid=invalid), self.assertRaises(TraceError):
                self.prepare(members=invalid)
        with self.assertRaises(TraceError):
            prepare_collection(self.first, self.members, hops=True)
        with self.assertRaises(TraceError):
            prepare_collection(self.first, self.members, hops=1, resume=read_case(dataset_path(self.first))["latest_run"])

    def test_invalid_first_selection_does_not_initialize_the_workspace_dataset(self):
        with self.assertRaises(TraceError):
            self.prepare(members=["f" * 32])
        self.assertFalse(dataset_path(self.first).exists())
        different = self.root / "different.json"
        save_json(different, {**fixture(), "/synthetic": {}})
        outsider = create_investigation(self.root, "Other", seeds=[A + ":0"], fixture=different)
        with self.assertRaises(TraceError):
            self.prepare(members=[*self.members, read_case(outsider)["case_id"]])
        self.assertFalse(dataset_path(self.first).exists())

    def test_shared_archive_symlinks_are_rejected_even_when_bytes_match(self):
        result = self.collect(self.prepare())
        _, _, archive = load_shared_run(self.first, result["run_id"])
        original = archive / "labels.json"
        outside = self.root / "identical-labels.json"
        outside.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(outside)
        with self.assertRaisesRegex(TraceError, "symbolic links"):
            load_shared_run(self.first, result["run_id"])


if __name__ == "__main__":
    unittest.main()
