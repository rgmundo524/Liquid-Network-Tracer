"""Hop progress observes bounded collection without altering its evidence."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.cli import main as cli_main
from liquid_tracer.common import save_json
from liquid_tracer.progress import COLLECTION_PHASES, ProgressReporter, public_progress
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import A, B, C, D, fixture
from tests.test_service_stops import network
from tests.test_trace_concurrency import RecordingTransport, converging_fixture, evidence_topology


class CollectionProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.counter = 0

    def collect(self, *, data=None, seeds=None, parent=None, only=None, limits=None,
                labels=(), workers=1, callback=None, fail_get=None):
        self.counter += 1
        directory = self.root / str(self.counter)
        limits = limits or Limits(max_hops=4)
        transport = RecordingTransport(fixture() if data is None else data)
        events, positions = [], []
        store = Store(directory)
        try:
            with Esplora(store, "pending", limits, auth="none", transport=transport,
                         min_interval=0, workers=workers, advertised_rps=1_000_000) as api:
                state = new_state(seeds or [A + ":0"], api.base, limits, list(labels), parent)
                api.run_id = state["run_id"]

                def observe(event):
                    events.append(copy.deepcopy(event))
                    positions.append({"event": copy.deepcopy(event), "calls": list(transport.calls),
                                      "links": set(state["links"]), "transactions": set(state["transactions"])})
                    if callback:
                        callback(event)

                with contextlib.ExitStack() as stack:
                    if fail_get:
                        stack.enter_context(patch.object(api, "get", side_effect=fail_get))
                    result = trace(api, state, limits, directory / "trace.json", only=only,
                                   progress=observe)
        finally:
            store.close()
        return result, transport.calls, events, positions

    def test_parallel_uneven_seeds_report_frontier_before_prefetch_not_first_child(self):
        data, roots, branches, joined, _ = converging_fixture(3)
        data["/tx/" + roots[0] + "/outspends"][0] = {"spent": False}
        for workers in (1, 8):
            with self.subTest(workers=workers):
                state, _, events, positions = self.collect(
                    data=data, seeds=[root + ":0" for root in roots], workers=workers,
                    limits=Limits(max_hops=2))
                active = [p for p in positions if p["event"]["phase"] == "collecting"]
                self.assertEqual([p["event"]["completed"] for p in active], [0, 1, 2])
                self.assertEqual(active[0]["calls"], [])
                self.assertEqual(active[1]["links"], {root + ":0" for root in roots[1:]})
                self.assertTrue(set(roots).issubset(active[1]["transactions"]))
                self.assertEqual(events[-1]["phase"], "collection_complete")
                self.assertTrue(all(event["total"] == 2 for event in events))
                self.assertEqual(state["status"], "bounded_complete")

    def test_resume_uses_active_frontier_and_cumulative_target(self):
        parent, _, _, _ = self.collect(limits=Limits(max_hops=1))
        original = copy.deepcopy(parent)
        state, _, events, _ = self.collect(parent=parent, limits=Limits(max_hops=3))
        self.assertEqual(events[0]["completed"], 1)
        self.assertTrue(all(event["total"] == 3 for event in events))
        self.assertEqual(events[-1]["completed"], 3)
        self.assertEqual(parent, original)
        self.assertEqual(state["status"], "bounded_complete")

        # A shallow unspent frontier must not begin at an unrelated deep branch.
        data = fixture()
        data["/tx/" + B + "/outspends"][1] = {"spent": False}
        parent, _, _, _ = self.collect(data=data)
        self.assertEqual(max(tx["depth"] for tx in parent["transactions"].values()), 3)
        _, _, events, _ = self.collect(data=data, parent=parent, only={B + ":1"},
                                      limits=Limits(max_hops=6))
        self.assertEqual((events[0]["completed"], events[0]["total"]), (1, 6))
        self.assertEqual(events[-1]["completed"], 1)

    def test_zero_hops_and_early_endpoint_do_not_invent_target_completion(self):
        state, calls, events, _ = self.collect(limits=Limits(max_hops=0))
        self.assertEqual(calls, ["/tx/" + A])
        self.assertEqual([(e["phase"], e["completed"], e["total"]) for e in events],
                         [("collecting", 0, 0), ("collection_complete", 0, 0)])
        _, _, events, _ = self.collect(limits=Limits(max_hops=10))
        self.assertEqual((events[-1]["completed"], events[-1]["total"]), (3, 10))
        _, _, events, _ = self.collect(labels=[{"kind": "address", "value": "SYNTHETIC-victim-deposit", "stop": True}])
        self.assertEqual(events[-1]["completed"], 0)

    def test_request_and_outpoint_limits_errors_and_interrupts_keep_actual_hop(self):
        for limits in (Limits(max_hops=9, max_requests=2), Limits(max_hops=9, max_outpoints=1)):
            with self.subTest(limits=limits):
                state, _, events, _ = self.collect(limits=limits)
                self.assertEqual(state["status"], "paused")
                self.assertEqual((events[-1]["phase"], events[-1]["completed"], events[-1]["total"]),
                                 ("collection_paused", 0, 9))
        data = fixture()
        data["/tx/" + A + "/outspends"][0]["vin"] = 999
        state, _, events, _ = self.collect(data=data)
        self.assertEqual(state["status"], "error")
        self.assertEqual((events[-1]["phase"], events[-1]["completed"]), ("collection_error", 0))
        state, _, events, _ = self.collect(fail_get=KeyboardInterrupt())
        self.assertEqual(state["stop_reason"], "interrupted")
        self.assertEqual((events[-1]["phase"], events[-1]["completed"]), ("collection_paused", 0))

    def test_observer_failure_preserves_evidence_requests_and_service_scope(self):
        ids, addresses, data = network()
        rule = {"kind": "address", "value": addresses["B"], "hop_limit": 1, "stop": False}

        def broken(event):
            event.clear()  # An observer receives no reference into trace evidence.
            raise RuntimeError("Synthetic failed observer")

        for workers in (1, 8):
            with self.subTest(workers=workers):
                options = {"data": data, "seeds": [ids["A"] + ":0"], "labels": [rule], "workers": workers}
                ordinary, calls, _, _ = self.collect(**options)
                failed, failed_calls, _, _ = self.collect(**options, callback=broken)
                self.assertEqual(evidence_topology(failed), evidence_topology(ordinary))
                self.assertEqual(Counter(failed_calls), Counter(calls))
                self.assertEqual(failed["stats"], ordinary["stats"])
                parent = copy.deepcopy(ordinary)
                resumed, requests, events, _ = self.collect(**options, parent=parent, callback=broken)
                self.assertEqual(requests, [])
                self.assertEqual(resumed["links"], parent["links"])
                self.assertEqual(events, [{"phase": "collection_empty", "completed": 0, "total": 0,
                                           "message": "No eligible outputs remain to collect"}])

    def test_cli_callback_flows_from_trace_to_counts_and_saved_archive(self):
        fixture_path = self.root / "api.json"
        save_json(fixture_path, fixture())
        events = []

        def counts(case, state, *, progress=None, **kwargs):
            self.assertTrue((case / "runs" / state["run_id"] / "trace.json").exists())
            self.assertEqual(events[-1]["phase"], "collection_complete")
            progress({"phase": "address_counts", "completed": 0, "total": 1})
            progress({"phase": "address_counts_ready", "completed": 1, "total": 1})
            return {}

        output = io.StringIO()
        with patch("liquid_tracer.cli.ensure_counts", side_effect=counts), contextlib.redirect_stdout(output):
            status = cli_main(["trace", "--case", str(self.root / "case"), "--seed", A + ":0",
                               "--hops", "1", "--fixture", str(fixture_path), "--min-interval", "0"],
                              progress=events.append)
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "bounded_complete")
        self.assertTrue((Path(result["directory"]) / "SHA256SUMS").exists())
        phases = [e["phase"] for e in events]
        self.assertEqual(phases[:3], ["collecting", "collecting", "collection_complete"])
        self.assertEqual(phases[-4:], ["address_counts", "address_counts_ready", "exporting_collection", "exporting_collection"])
        self.assertEqual([(e["completed"], e["total"]) for e in events[-2:]], [(0, 1), (1, 1)])

    def test_failed_cli_keeps_stopped_hop_while_saving_partial_archive(self):
        data = fixture()
        data["/tx/" + A + "/outspends"][0]["vin"] = 999
        fixture_path = self.root / "bad-api.json"
        save_json(fixture_path, data)
        events, output = [], io.StringIO()
        with patch("liquid_tracer.cli.ensure_counts", side_effect=AssertionError("No counts after trace error")), \
                contextlib.redirect_stdout(output):
            status = cli_main(["trace", "--case", str(self.root / "case"), "--seed", A + ":0",
                               "--hops", "4", "--fixture", str(fixture_path), "--min-interval", "0"],
                              progress=events.append)
        self.assertEqual(status, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(events[-1]["phase"], "collection_error")
        self.assertEqual(events[-1]["completed"], 0)
        self.assertTrue((Path(result["directory"]) / "SHA256SUMS").exists())


class CollectionProgressBoundaryTests(unittest.TestCase):
    def test_hop_phases_are_sanitized_and_idempotent(self):
        for phase in (*COLLECTION_PHASES, "exporting_collection"):
            for completed, total in ((0, 0), (0, 10), (3, 10)):
                with self.subTest(phase=phase, completed=completed, total=total):
                    event = public_progress({"phase": phase, "completed": completed, "total": total,
                                             "message": "PRIVATE", "address": "PRIVATE", "txid": "PRIVATE"})
                    self.assertIsNotNone(event)
                    self.assertEqual(public_progress(event), event)
                    self.assertNotIn("PRIVATE", json.dumps(event))
                    for bad in (True, -1, "3", 2.5, 2 ** 53):
                        self.assertIsNone(public_progress({**event, "completed": bad}))

    def test_hop_changes_are_immediate_and_same_boundary_is_not_spammed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            stderr = io.StringIO()
            with patch("liquid_tracer.progress.time.monotonic", side_effect=[0, .01, .02, .03, .04, .05]), \
                    contextlib.redirect_stderr(stderr):
                reporter = ProgressReporter(path)
                for hop in (0, 1, 2, 1, 2):
                    reporter({"phase": "collecting", "completed": hop, "total": 2})
                    self.assertEqual(json.loads(path.read_text())["completed"], hop)
                reporter({"phase": "collecting", "completed": 2, "total": 2})
            self.assertEqual(len(stderr.getvalue().splitlines()), 5)
            self.assertTrue(all(line.startswith("Collection: ") for line in stderr.getvalue().splitlines()))
            self.assertIn("Processing hop 2 of 2", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
