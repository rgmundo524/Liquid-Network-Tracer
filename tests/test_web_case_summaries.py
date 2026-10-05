"""Case lists read only the selected collection snapshot, never plot archives."""

from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from liquid_tracer.common import read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.web import LocalServer, RequestError


LATEST = "a" * 16
OLDER = "b" * 16
PROJECTED = "c" * 16
SEED = "1" * 64 + ":0"


class CaseSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Exercise the real summary/session methods without binding a socket.
        self.server = LocalServer.__new__(LocalServer)
        self.server.root = self.root
        self.server.csrf = "synthetic-session-token"
        self.server.job_lock = threading.RLock()
        self.server.jobs = {}
        self.case = self.create_case("Synthetic summary", LATEST)

    def create_case(self, name, latest):
        case = create_investigation(self.root, name, seeds=[SEED])
        save_json(case / "case.json", {**read_case(case), "latest_run": latest})
        return case

    def archive(self, run=LATEST, *, case=None, **changes):
        case = self.case if case is None else case
        directory = case / "runs" / run
        directory.mkdir(parents=True, exist_ok=True)
        state = {"case_id": read_case(case)["case_id"], "run_id": run,
                 "status": "bounded_complete", "stop_reason": "max_hops",
                 "started_at": "2026-01-02T00:00:00Z", "seeds": [SEED],
                 "transactions": {"first": {"depth": 0}, "last": {"depth": 2}},
                 "stats": {"transactions_cumulative": 7, "frontier_count": 3},
                 "limits": {"max_hops": 4},
                 "performance": {"schema_version": 1, "request_count": 5,
                                 "private_value": "must not be exposed"}}
        state.update(changes)
        save_json(directory / "trace.json", state)
        (directory / "SHA256SUMS").write_text("synthetic seal\n", encoding="utf-8")
        return directory

    @contextmanager
    def list_reads(self, allowed=()):
        """Fail at the access boundary instead of allocating large trace data."""
        allowed = set(allowed)
        reads = []
        original_iterdir = Path.iterdir

        def iterdir(path):
            if path.name == "runs":
                raise AssertionError("A case list must not enumerate run archives")
            return original_iterdir(path)

        def read(path):
            path = Path(path)
            self.assertIn(path, allowed, "A case list read an unrelated archive")
            reads.append(path)
            return read_json(path)

        with patch.object(Path, "iterdir", iterdir), \
                patch("liquid_tracer.web.read_json", side_effect=read):
            yield reads

    @contextmanager
    def detail_dependencies(self):
        # Isolate collection history from the other detail tabs and products.
        with ExitStack() as stack:
            artifacts = stack.enter_context(patch.object(self.server, "saved_artifacts", return_value={}))
            stack.enter_context(patch.object(self.server, "pegout_searches", return_value=[]))
            stack.enter_context(patch.object(self.server, "shared_collection_summary", return_value={}))
            stack.enter_context(patch("liquid_tracer.workflow_api.case_workflow", return_value={}))
            stack.enter_context(patch("liquid_tracer.cli.miro_recovery_status", return_value=None))
            stack.enter_context(patch("liquid_tracer.board_rebuild.rebuild_status", return_value=None))
            yield artifacts

    def test_session_reads_cached_latest_and_skips_cases_without_a_selection(self):
        latest = self.archive()
        self.archive(OLDER)
        projected = self.archive(PROJECTED, collection_source={"kind": "shared"})
        no_latest = self.create_case("Projected snapshots only", None)
        self.archive(PROJECTED, case=no_latest, collection_source={"kind": "shared"})
        # A large unselected archive must not even reach the JSON reader.
        with (projected / "trace.json").open("r+b") as stream:
            stream.truncate(256 * 1024 * 1024)
        from liquid_tracer.run_summaries import remember_run_summary
        self.assertTrue(remember_run_summary(latest, read_json(latest / "trace.json")))
        with self.list_reads() as reads:
            session = self.server.session()
        self.assertEqual(reads, [])
        cases = {case["id"]: case for case in session["cases"]}
        summary = cases[read_case(self.case)["case_id"]]
        self.assertEqual(summary["status"], "bounded_complete")
        self.assertEqual(summary["latest"], {
            "id": LATEST, "status": "bounded_complete", "stop_reason": "max_hops",
            "created_at": "2026-01-02T00:00:00Z", "transaction_count": 7,
            "frontier_count": 3, "performance": {"schema_version": 1, "request_count": 5},
            "max_hops": 4, "collected_hops": 2})
        self.assertNotIn("runs", summary)
        self.assertNotIn("seeds", summary["latest"])
        self.assertEqual(cases[read_case(no_latest)["case_id"]]["status"], "Not started")
        self.assertEqual(session["active_jobs"], [])

    def test_missing_or_malformed_selection_does_not_scan_or_fall_back(self):
        self.archive(OLDER)
        metadata = read_case(self.case)
        for latest in (None, "", "too-short", "../" + OLDER, 123, [], {}, [LATEST], {"run": LATEST}):
            with self.subTest(latest=latest), self.list_reads() as reads:
                summary = self.server.case_summary(self.case, {**metadata, "latest_run": latest})
                self.assertEqual(summary["status"], "Saved run unavailable" if latest else "Not started")
                self.assertNotIn("latest", summary)
                self.assertEqual(reads, [])
        metadata.pop("latest_run")
        with self.list_reads():
            self.assertEqual(self.server.case_summary(self.case, metadata)["status"], "Not started")

    def test_unavailable_selected_snapshot_never_falls_back_to_history(self):
        variants = ("missing", "unsealed", "wrong-case", "wrong-run", "shared", "bad-stats",
                    "not-object", "bad-json", "symlink", "not-directory")
        for variant in variants:
            with self.subTest(variant=variant):
                case = self.create_case(variant, LATEST)
                older = self.archive(OLDER, case=case)
                allowed = []
                if variant == "symlink":
                    (case / "runs" / LATEST).symlink_to(older, target_is_directory=True)
                elif variant == "not-directory":
                    (case / "runs" / LATEST).write_text("not a directory")
                elif variant != "missing":
                    selected = self.archive(case=case)
                    trace = selected / "trace.json"
                    if variant == "unsealed":
                        (selected / "SHA256SUMS").unlink()
                    else:
                        allowed.append(trace)
                        state = read_json(trace)
                        if variant == "wrong-case":
                            state["case_id"] = "0" * 32
                        elif variant == "wrong-run":
                            state["run_id"] = OLDER
                        elif variant == "shared":
                            state["collection_source"] = {"kind": "shared"}
                        elif variant == "bad-stats":
                            state["stats"] = []
                        elif variant == "not-object":
                            state = []
                        save_json(trace, state)
                        if variant == "bad-json":
                            trace.write_text("{broken JSON")
                with self.list_reads(allowed) as reads:
                    summary = self.server.case_summary(case, read_case(case))
                self.assertEqual(summary["status"], "Saved run unavailable")
                self.assertNotIn("latest", summary)
                self.assertEqual(reads, allowed)

    def test_selected_trace_symlink_still_rejected_by_safe_path(self):
        latest, older = self.archive(), self.archive(OLDER)
        (latest / "trace.json").unlink()
        (latest / "trace.json").symlink_to(older / "trace.json")
        with self.list_reads(), self.assertRaises(RequestError):
            self.server.case_summary(self.case, read_case(self.case))

    def test_existing_alphanumeric_run_ids_and_named_depth_are_preserved(self):
        selected = "Z9" * 8
        self.archive(selected, hop_reference_name=" Treasury ",
                     transactions={"first": {"depth": 10, "reference_hops": 1},
                                   "last": {"depth": 12, "reference_hops": 3}})
        metadata = {**read_case(self.case), "latest_run": selected}
        with self.list_reads([self.case / "runs" / selected / "trace.json"]):
            summary = self.server.case_summary(self.case, metadata)
        self.assertEqual(summary["latest"]["id"], selected)
        self.assertEqual(summary["latest"]["hop_reference_name"], "Treasury")
        self.assertEqual(summary["latest"]["collected_hops"], 3)

    def test_detail_keeps_private_history_and_artifact_run_set(self):
        latest = self.archive()
        older = self.archive(OLDER, started_at="2026-01-01T00:00:00Z", seeds=[SEED, SEED])
        projected = self.archive(PROJECTED, collection_source={"kind": "shared"})
        metadata = read_case(self.case)
        with self.detail_dependencies() as artifacts, \
                patch("liquid_tracer.web.read_json", wraps=read_json) as read:
            detail = self.server.case_summary(self.case, metadata, detail=True)
        self.assertEqual({call.args[0] for call in read.call_args_list},
                         {path / "trace.json" for path in (latest, older, projected)})
        self.assertEqual([run["id"] for run in detail["runs"]], [LATEST, OLDER])
        self.assertEqual(detail["latest"], detail["runs"][0])
        self.assertTrue(all(run["seeds"] == [SEED] for run in detail["runs"]))
        self.assertEqual(detail["seeds"], [SEED])
        artifacts.assert_called_once_with(self.case, metadata, {LATEST, OLDER})
        with self.detail_dependencies():
            detail = self.server.case_summary(self.case, {**metadata, "latest_run": "d" * 16}, detail=True)
        self.assertEqual(detail["status"], "Saved run unavailable")
        self.assertEqual([run["id"] for run in detail["runs"]], [LATEST, OLDER])
        self.assertNotIn("latest", detail)


if __name__ == "__main__":
    unittest.main()
