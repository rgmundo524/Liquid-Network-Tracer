"""Trace-level bounds and evidence topology under parallel API fetching."""

import copy
import hashlib
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path

from liquid_tracer.api import ENTERPRISE, Esplora, Limits
from liquid_tracer.common import LBTC, canonical
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import CONFIRMED, output


def converging_fixture(count=8):
    """Independent selected roots and branches converge into one transaction."""
    def txid(name):
        return hashlib.sha256(("SYNTHETIC-concurrency-" + name).encode()).hexdigest()

    roots = sorted(txid("root-" + str(index)) for index in range(count))
    branches = [txid("branch-" + str(index)) for index in range(count)]
    joined, unrelated = txid("joined"), txid("unrelated-context")
    data, joined_inputs = {}, []
    for index, (root, branch) in enumerate(zip(roots, branches)):
        selected = output("SYNTHETIC-root-" + str(index))
        descendant = output("SYNTHETIC-branch-" + str(index))
        data["/tx/" + root] = {"txid": root, "status": dict(CONFIRMED),
            "vin": [{"txid": unrelated, "vout": index,
                     "prevout": output("SYNTHETIC-prior-context")}],
            "vout": [selected, output("SYNTHETIC-unselected-sibling")]}
        data["/tx/" + root + "/outspends"] = [
            {"spent": True, "txid": branch, "vin": 0, "status": dict(CONFIRMED)},
            {"spent": True, "txid": unrelated, "vin": index, "status": dict(CONFIRMED)}]
        data["/tx/" + branch] = {"txid": branch, "status": dict(CONFIRMED),
            "vin": [{"txid": root, "vout": 0, "prevout": selected}],
            "vout": [descendant]}
        data["/tx/" + branch + "/outspends"] = [
            {"spent": True, "txid": joined, "vin": index, "status": dict(CONFIRMED)}]
        joined_inputs.append({"txid": branch, "vout": 0, "prevout": descendant})
    data["/tx/" + joined] = {"txid": joined, "status": dict(CONFIRMED),
        "vin": joined_inputs, "vout": [output("SYNTHETIC-merged-output")]}
    return data, roots, branches, joined, unrelated


class RecordingTransport:
    """Synthetic responses with optional latency or a deterministic overlap gate."""

    def __init__(self, data, delay=0, barrier_endpoints=()):
        self.data, self.delay = data, delay
        self.calls = []
        self.active = self.maximum_active = 0
        self.lock = threading.Lock()
        self.barrier_endpoints = set(barrier_endpoints)
        self.barrier = threading.Barrier(len(self.barrier_endpoints)) if self.barrier_endpoints else None

    def __call__(self, method, url, headers, body, timeout):
        if method != "GET" or not url.startswith(ENTERPRISE + "/"):
            raise AssertionError("Unexpected synthetic request")
        endpoint = url.removeprefix(ENTERPRISE)
        with self.lock:
            self.calls.append(endpoint)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            if self.barrier is not None and endpoint in self.barrier_endpoints:
                self.barrier.wait(timeout=5)
            if self.delay:
                time.sleep(self.delay)
            # Missing endpoints fail the test, including unselected siblings,
            # prior funding, and requests beyond the supplied hop boundary.
            return 200, {}, canonical(self.data[endpoint])
        finally:
            with self.lock:
                self.active -= 1


def evidence_topology(state):
    """Compare investigative data while allowing separate observation identities."""
    def without_observation_ids(value):
        if isinstance(value, dict):
            return {key: without_observation_ids(item) for key, item in value.items()
                    if not key.endswith("observation_id")}
        if isinstance(value, list):
            return [without_observation_ids(item) for item in value]
        return value

    return without_observation_ids({key: state[key] for key in
        ("seeds", "transactions", "outputs", "links", "status", "stop_reason", "errors")})


def synthetic_trace(directory, data, seeds, *, workers=8, limits=None, labels=(),
                    parent=None, only=None, delay=0, advertised_rps=1_000_000,
                    barrier_endpoints=()):
    """Shared harness for assertions and a separately invoked synthetic benchmark."""
    directory = Path(directory)
    limits = limits or Limits(max_hops=2)
    transport = RecordingTransport(data, delay, barrier_endpoints)
    store = Store(directory)
    try:
        with Esplora(store, "pending", limits, auth="none", transport=transport,
                     min_interval=0, workers=workers, advertised_rps=advertised_rps) as api:
            state = new_state(seeds, api.base, limits, list(labels), parent)
            api.run_id = state["run_id"]
            result = trace(api, state, limits, directory / state["run_id"] / "trace.json", only=only)
    finally:
        store.close()
    if transport.active:
        raise AssertionError("Trace returned before its synthetic requests finished")
    return result, transport


class TraceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data, self.roots, self.branches, self.joined, self.unrelated = converging_fixture()
        self.seeds = [txid + ":0" for txid in self.roots]
        self.counter = 0

    def run_trace(self, workers, **options):
        self.counter += 1
        return synthetic_trace(self.root / str(self.counter), self.data, self.seeds,
                               workers=workers, **options)

    def test_parallel_trace_overlaps_and_matches_serial_evidence_without_duplicate_requests(self):
        serial, sequential = self.run_trace(1)
        parallel, concurrent = self.run_trace(8,
            barrier_endpoints=["/tx/" + txid for txid in self.roots])
        self.assertEqual(serial["status"], "bounded_complete")
        self.assertEqual(evidence_topology(parallel), evidence_topology(serial))
        self.assertEqual(Counter(concurrent.calls), Counter(sequential.calls))
        self.assertTrue(all(count == 1 for count in Counter(concurrent.calls).values()))
        self.assertEqual(concurrent.maximum_active, 8)
        self.assertEqual(sequential.maximum_active, 1)
        self.assertEqual(len(concurrent.calls), 33)
        self.assertEqual(parallel["stats"]["requests_this_run"], 33)
        self.assertEqual(len(parallel["observations"]), 33)
        self.assertEqual(concurrent.calls.count("/tx/" + self.joined), 1)
        self.assertEqual(len(parallel["links"]), 16)
        self.assertEqual({parallel["links"][txid + ":0"]["vin"] for txid in self.branches}, set(range(8)))
        self.assertNotIn(self.unrelated, parallel["transactions"])
        self.assertTrue(all(txid + ":1" not in parallel["outputs"] for txid in self.roots))

    def test_hop_boundaries_fetch_only_required_funding_and_spending_transactions(self):
        for hops in (0, 1):
            expected = {"/tx/" + txid for txid in self.roots}
            if hops:
                expected.update("/tx/" + txid + "/outspends" for txid in self.roots)
                expected.update("/tx/" + txid for txid in self.branches)
            for workers in (1, 8):
                with self.subTest(hops=hops, workers=workers):
                    state, transport = self.run_trace(workers, limits=Limits(max_hops=hops))
                    self.assertEqual(state["status"], "bounded_complete")
                    self.assertCountEqual(transport.calls, expected)
                    self.assertNotIn(self.joined, state["transactions"])

    def test_tight_transaction_request_and_outpoint_caps_do_not_spend_on_later_frontier_items(self):
        for options, reason in (({"max_transactions": 1}, "transaction_limit"),
                                ({"max_transactions": 10}, "transaction_limit"),
                                ({"max_requests": 2}, "request_limit"),
                                ({"max_requests": 9}, "request_limit"),
                                ({"max_outpoints": 1}, "outpoint_limit"),
                                ({"max_outpoints": 3}, "outpoint_limit")):
            with self.subTest(options=options):
                limits = Limits(max_hops=2, **options)
                serial, sequential = self.run_trace(1, limits=limits)
                parallel, concurrent = self.run_trace(8, limits=limits)
                self.assertEqual(parallel["stop_reason"], reason)
                self.assertEqual(evidence_topology(parallel), evidence_topology(serial))
                self.assertEqual(Counter(concurrent.calls), Counter(sequential.calls))
                self.assertTrue(all(count == 1 for count in Counter(concurrent.calls).values()))
                self.assertLessEqual(len(concurrent.calls), limits.max_requests)
                fetched_transactions = [endpoint for endpoint in concurrent.calls
                                        if not endpoint.endswith("/outspends")]
                self.assertLessEqual(len(fetched_transactions), limits.max_transactions)
                self.assertLessEqual(parallel["stats"]["outpoints_examined_this_run"], limits.max_outpoints)

    def test_stops_terminal_outputs_and_unconfirmed_activity_are_not_expanded_by_prefetch(self):
        roots = self.roots
        labels = [{"kind": "outpoint", "value": roots[0] + ":0", "stop": True}]
        self.data["/tx/" + roots[1]]["status"] = {"confirmed": False}
        self.data["/tx/" + roots[2]]["vout"][0] = {
            "scriptpubkey": "", "scriptpubkey_type": "fee", "value": 100, "asset": LBTC}
        self.data["/tx/" + roots[3]]["vout"][0] = {
            "scriptpubkey": "6a", "scriptpubkey_type": "op_return", "pegout": {"genesis_hash": "00" * 32}}
        self.data["/tx/" + roots[4]]["vout"][0] = {
            "scriptpubkey": "6a00", "scriptpubkey_type": "op_return"}
        self.data["/tx/" + roots[5] + "/outspends"][0]["status"] = {"confirmed": False}
        self.data["/tx/" + roots[6] + "/outspends"][0] = {"spent": False}
        expected = {"/tx/" + txid for txid in roots}
        expected.update("/tx/" + txid + "/outspends" for txid in roots[5:])
        expected.add("/tx/" + self.branches[7])
        serial, _ = self.run_trace(1, labels=labels, limits=Limits(max_hops=1))
        parallel, transport = self.run_trace(8, labels=labels, limits=Limits(max_hops=1))
        self.assertEqual(evidence_topology(parallel), evidence_topology(serial))
        self.assertCountEqual(transport.calls, expected)
        self.assertEqual(parallel["outputs"][roots[0] + ":0"]["status"], "analyst_stop")
        self.assertEqual(parallel["outputs"][roots[1] + ":0"]["status"], "unconfirmed_funding")
        self.assertEqual(set(parallel["links"]), {roots[7] + ":0"})

    def test_selected_continuation_fetches_only_selected_branches_and_shared_descendant(self):
        case = self.root / "continued"
        parent, _ = synthetic_trace(case, self.data, self.seeds, limits=Limits(max_hops=0))
        original = copy.deepcopy(parent)
        selected = {txid + ":0" for txid in self.roots[:2]}
        state, transport = synthetic_trace(case, self.data, self.seeds, parent=parent, only=selected)
        expected = {"/tx/" + txid + "/outspends" for txid in self.roots[:2] + self.branches[:2]}
        expected.update("/tx/" + txid for txid in self.branches[:2] + [self.joined])
        self.assertCountEqual(transport.calls, expected)
        self.assertEqual(parent, original)
        self.assertEqual(state["status"], "bounded_complete")
        self.assertEqual(set(state["links"]), selected | {txid + ":0" for txid in self.branches[:2]})
        self.assertTrue(all(state["outputs"][txid + ":0"]["status"] == "hop_limit"
                            for txid in self.roots[2:]))
        self.assertTrue(all(txid not in state["transactions"] for txid in self.branches[2:]))

    def test_later_malformed_spend_does_not_erase_earlier_valid_trace_progress(self):
        malformed = self.roots[1]
        self.data["/tx/" + malformed + "/outspends"][0]["status"] = None
        serial, _ = self.run_trace(1)
        parallel, transport = self.run_trace(8)
        self.assertEqual(serial["status"], "error")
        self.assertEqual(parallel["status"], "error")
        self.assertIn(self.roots[0] + ":0", serial["links"])
        self.assertEqual(evidence_topology(parallel), evidence_topology(serial))
        self.assertNotIn("/tx/" + self.branches[1], transport.calls)


if __name__ == "__main__":
    unittest.main()
