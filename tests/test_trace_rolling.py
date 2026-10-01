"""A slow lookahead request must not drain the whole trace pipeline."""

import copy
import tempfile
import threading
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from liquid_tracer.api import ENTERPRISE, Esplora, Limits
from liquid_tracer.common import match_labels, read_json, save_json
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from liquid_tracer.trace_checkpoint import TraceCheckpoint
from liquid_tracer.trace_fetch import FrontierFetcher
from tests.test_trace_concurrency import (RecordingTransport, converging_fixture,
                                          evidence_topology, synthetic_trace)


class RollingTraceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data, self.roots, self.branches, self.joined, self.unrelated = converging_fixture(8)
        self.seeds = [txid + ":0" for txid in self.roots]

    def collect(self, directory, transport, *, limits=None, labels=(), parent=None, only=None):
        limits = limits or Limits(max_hops=2)
        directory = self.root / directory
        with closing(Store(directory)) as store:
            with Esplora(store, "pending", limits, auth="none", transport=transport,
                         workers=2, adaptive_workers=False, advertised_rps=1_000_000) as api:
                state = new_state(self.seeds, api.base, limits, list(labels), parent)
                api.run_id = state["run_id"]
                result = trace(api, state, limits, directory / "trace.json", only=only)
                self.assertTrue(all(future.done() for future in api._results.values()))
        return result

    def test_slow_tail_does_not_block_commit_or_replacement_frontier_request(self):
        """Two workers replenish four-item lookahead while its second item waits."""
        slow_entered, release_slow = threading.Event(), threading.Event()
        committed, replacement_started = threading.Event(), threading.Event()
        recorded = RecordingTransport(self.data)
        first_key = self.seeds[0]

        def transport(method, url, headers, body, timeout):
            endpoint = url.removeprefix(ENTERPRISE)
            if endpoint == "/tx/" + self.roots[1]:
                slow_entered.set()
                if not release_slow.wait(10):
                    raise AssertionError("Test did not release the held funding request")
            if endpoint == "/tx/" + self.roots[4]:
                replacement_started.set()
            return recorded(method, url, headers, body, timeout)

        # A real checkpoint after each completed output makes committed
        # traversal visible without polling state or relying on elapsed time.
        def checkpoint(path, state):
            if first_key in state["links"]:
                committed.set()
            save_json(path, state)

        with patch("liquid_tracer.trace.TraceCheckpoint", side_effect=lambda: TraceCheckpoint(max_operations=1)), \
             patch("liquid_tracer.trace.save_json", side_effect=checkpoint), \
             ThreadPoolExecutor(max_workers=1) as runner:
            future = runner.submit(self.collect, "rolling", transport)
            try:
                self.assertTrue(slow_entered.wait(5), "Second funding request did not start")
                self.assertTrue(committed.wait(5), "Ready output waited for the slow lookahead tail")
                self.assertTrue(replacement_started.wait(5),
                                "Consumed output did not replenish the lookahead before its tail finished")
                self.assertFalse(release_slow.is_set())
            finally:
                release_slow.set()
            result = future.result(timeout=10)

        serial, serial_transport = synthetic_trace(self.root / "serial", self.data, self.seeds, workers=1)
        self.assertEqual(result["status"], "bounded_complete")
        self.assertEqual(evidence_topology(result), evidence_topology(serial))
        self.assertEqual(Counter(recorded.calls), Counter(serial_transport.calls))
        self.assertTrue(all(count == 1 for count in Counter(recorded.calls).values()))

    def test_network_request_finishes_while_committed_state_is_checkpointing(self):
        network_entered, release_network = threading.Event(), threading.Event()
        checkpoint_entered, release_checkpoint = threading.Event(), threading.Event()
        network_finished = threading.Event()
        first_key = self.seeds[0]
        recorded = RecordingTransport(self.data)

        def transport(method, url, headers, body, timeout):
            endpoint = url.removeprefix(ENTERPRISE)
            if endpoint == "/tx/" + self.roots[1]:
                network_entered.set()
                if not release_network.wait(10):
                    raise AssertionError("Test did not release the active request")
                response = recorded(method, url, headers, body, timeout)
                network_finished.set()
                return response
            return recorded(method, url, headers, body, timeout)

        def checkpoint(path, state):
            if first_key in state["links"] and not checkpoint_entered.is_set():
                checkpoint_entered.set()
                if not release_checkpoint.wait(10):
                    raise AssertionError("Test did not release the checkpoint")
            save_json(path, state)

        with patch("liquid_tracer.trace.TraceCheckpoint", side_effect=lambda: TraceCheckpoint(max_operations=1)), \
             patch("liquid_tracer.trace.save_json", side_effect=checkpoint), \
             ThreadPoolExecutor(max_workers=1) as runner:
            future = runner.submit(self.collect, "checkpoint", transport)
            try:
                self.assertTrue(network_entered.wait(5))
                self.assertTrue(checkpoint_entered.wait(5),
                                "Checkpoint was delayed until every prefetched request drained")
                release_network.set()
                self.assertTrue(network_finished.wait(5), "Checkpoint blocked the pending network worker")
                self.assertFalse(release_checkpoint.is_set())
            finally:
                release_network.set()
                release_checkpoint.set()
            result = future.result(timeout=10)
        self.assertEqual(result["status"], "bounded_complete")
        self.assertEqual(len(recorded.calls), len(set(recorded.calls)))

    def test_many_refills_keep_serial_scope_and_do_not_follow_stopped_or_unselected_outputs(self):
        labels = [{"kind": "outpoint", "value": key, "stop": True}
                  for key in self.seeds[1::3]]
        limits = Limits(max_hops=1)
        serial, serial_transport = synthetic_trace(self.root / "serial-stops", self.data, self.seeds,
                                                   workers=1, limits=limits, labels=labels)
        recorded = RecordingTransport(self.data)
        result = self.collect("rolling-stops", recorded, limits=limits, labels=labels)
        self.assertEqual(evidence_topology(result), evidence_topology(serial))
        self.assertEqual(Counter(recorded.calls), Counter(serial_transport.calls))
        self.assertNotIn("/tx/" + self.joined, recorded.calls)
        self.assertNotIn("/tx/" + self.unrelated, recorded.calls)
        for label in labels:
            self.assertNotIn("/tx/" + label["value"].split(":")[0] + "/outspends", recorded.calls)

    def test_selected_continuation_refills_only_the_selected_saved_frontier(self):
        parent, _ = synthetic_trace(self.root / "parent", self.data, self.seeds,
                                     workers=1, limits=Limits(max_hops=0))
        original = copy.deepcopy(parent)
        selected = set(self.seeds[:5])
        serial, serial_transport = synthetic_trace(self.root / "serial-resume", self.data, self.seeds,
                                                   workers=1, parent=parent, only=selected)
        recorded = RecordingTransport(self.data)
        result = self.collect("rolling-resume", recorded, parent=parent, only=selected)
        self.assertEqual(parent, original)
        self.assertEqual(evidence_topology(result), evidence_topology(serial))
        self.assertEqual(Counter(recorded.calls), Counter(serial_transport.calls))
        self.assertEqual(set(result["links"]), selected | {txid + ":0" for txid in self.branches[:5]})
        for txid in self.roots[5:]:
            self.assertNotIn("/tx/" + txid + "/outspends", recorded.calls)

    def test_checkpoint_interrupt_wakes_shared_admission_before_final_snapshot(self):
        child_received, admission_waiting = threading.Event(), threading.Event()
        interrupted, trace_returned = threading.Event(), threading.Event()
        recorded = RecordingTransport(self.data)
        directory = self.root / "interrupted"
        checkpoint_path = directory / "trace.json"

        def reserve():
            blocked = child_received.is_set()
            if blocked:
                admission_waiting.set()
            return SimpleNamespace(admitted=not blocked, wait_seconds=10. if blocked else 0.,
                                   active_clients=2, effective_rps=49.,
                                   reason="server_cooldown" if blocked else None)

        quota = SimpleNamespace(reserve=reserve, close=lambda: None)

        def transport(method, url, headers, body, timeout):
            self.assertFalse(api._cancelled.is_set(), "Request started after cancellation")
            self.assertFalse(trace_returned.is_set(), "Request started after trace returned")
            response = recorded(method, url, headers, body, timeout)
            if url.removeprefix(ENTERPRISE) == "/tx/" + self.branches[0]:
                child_received.set()
            return response

        limits = Limits(max_hops=2)
        with closing(Store(directory)) as store, \
             Esplora(store, "pending", limits, auth="none", transport=transport,
                     workers=2, advertised_rps=1_000_000, shared_quota=quota) as api:
            state = new_state(self.seeds, api.base, limits, [])
            api.run_id = state["run_id"]
            original_admit, original_observe = api._admit, store.observe

            def admit(kind=None, endpoint=None):
                # Allow the first ordered output's dependency chain through,
                # then make its peer encounter a shared provider cooldown.
                if endpoint == "/tx/" + self.roots[1]:
                    if not child_received.wait(5):
                        raise AssertionError("First output never received its spending transaction")
                return original_admit(kind, endpoint)

            def observe(*args):
                self.assertFalse(trace_returned.is_set(), "Evidence was written after trace returned")
                return original_observe(*args)

            def checkpoint(path, value):
                if self.seeds[0] in value["links"] and not interrupted.is_set():
                    self.assertTrue(admission_waiting.wait(5), "Peer never reached shared admission")
                    interrupted.set()
                    raise KeyboardInterrupt()
                if value["status"] == "paused":
                    self.assertTrue(api._cancelled.is_set())
                    self.assertTrue(all(future.done() for future in api._results.values()),
                                    "Final checkpoint preceded completion of active workers")
                save_json(path, value)

            with patch.object(api, "_admit", side_effect=admit), \
                 patch.object(store, "observe", side_effect=observe), \
                 patch("liquid_tracer.trace.TraceCheckpoint", side_effect=lambda: TraceCheckpoint(max_operations=1)), \
                 patch("liquid_tracer.trace.save_json", side_effect=checkpoint), \
                 ThreadPoolExecutor(max_workers=1) as runner:
                future = runner.submit(trace, api, state, limits, checkpoint_path)
                try:
                    self.assertTrue(interrupted.wait(5), "Ordered checkpoint was never interrupted")
                    result = future.result(timeout=3)
                    trace_returned.set()
                finally:
                    # A regression must fail rather than hang test shutdown on
                    # the deliberately unbounded provider admission wait.
                    api.close()
            required = {"/tx/" + self.roots[0], "/tx/" + self.roots[0] + "/outspends",
                        "/tx/" + self.branches[0]}
            self.assertTrue(required.issubset(recorded.calls))
            self.assertNotIn("/tx/" + self.roots[1], recorded.calls)
            self.assertEqual(len(recorded.calls), len(set(recorded.calls)))
            self.assertTrue(all(future.done() for future in api._results.values()))
            observations = list(store.observations(api.used))
            self.assertCountEqual([row["endpoint"] for row in observations], recorded.calls)
            self.assertEqual(set(result["observations"]), set(api.used))
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["stop_reason"], "interrupted")
        self.assertEqual(set(result["links"]), {self.seeds[0]})
        self.assertEqual(evidence_topology(read_json(checkpoint_path)), evidence_topology(result))
        self.assertEqual(result["performance"]["checkpoint_count"], 2)

    def test_deadline_stops_ordered_consumption_of_an_already_prefetched_outspend(self):
        directory = self.root / "expired"
        checkpoint_path = directory / "trace.json"
        limits = Limits(max_hops=2, max_seconds=60)
        recorded = RecordingTransport(self.data)
        fetchers, expired = [], threading.Event()
        endpoint = "/tx/" + self.roots[0] + "/outspends"

        def fetcher(*args):
            value = FrontierFetcher(*args)
            fetchers.append(value)
            return value

        with closing(Store(directory)) as store, \
             Esplora(store, "pending", limits, auth="none", transport=recorded,
                     workers=2, advertised_rps=1_000_000) as api:
            state = new_state(self.seeds, api.base, limits, [])
            api.run_id = state["run_id"]

            def labels(*args):
                result = match_labels(*args)
                if args[1] == self.seeds[0] and self.roots[0] in state["transactions"] and not expired.is_set():
                    expired.set()
                    # The ordered consumer must honor the deadline even when
                    # the endpoint result no longer needs API.get or submit.
                    fetchers[0].get(endpoint)
                    self.assertIn(endpoint, fetchers[0].results)
                    api.budget.started -= limits.max_seconds + 1
                return result

            with patch("liquid_tracer.trace.FrontierFetcher", side_effect=fetcher), \
                 patch("liquid_tracer.trace.match_labels", side_effect=labels):
                result = trace(api, state, limits, checkpoint_path)
            self.assertTrue(all(future.done() for future in api._results.values()))
        self.assertTrue(expired.is_set())
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["stop_reason"], "time_limit")
        self.assertNotIn("spend_observation_id", result["outputs"][self.seeds[0]])
        self.assertEqual(result["links"], {})
        self.assertEqual(recorded.calls.count(endpoint), 1)
        self.assertEqual(evidence_topology(read_json(checkpoint_path)), evidence_topology(result))


if __name__ == "__main__":
    unittest.main()
