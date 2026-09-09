import copy
import hashlib
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import Budget, Esplora, Limits
from liquid_tracer.common import StopRun, TraceError, canonical, output_kind, quantity, read_json, save_json
from liquid_tracer.export import build_graph, export_run, svg_graph
from liquid_tracer.miro import make_plan, publish, resolve
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import A, B, C, D, X, CONFIRMED, fixture, output


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "case")
        self.fixture_file = self.root / "fixture.json"
        save_json(self.fixture_file, fixture())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_trace(self, hops=3, parent=None, labels=None, only=None, **kwargs):
        limits = Limits(max_hops=hops, **kwargs)
        api = Esplora(self.store, "pending", limits, fixture=self.fixture_file, min_interval=0)
        state = new_state([A + ":0"], api.base, limits, labels or [], parent)
        api.run_id = state["run_id"]
        return trace(api, state, limits, self.root / state["run_id"] / "trace.json", only=only)

    def test_exact_outpoint_no_seed_sibling_or_coinput_expansion(self):
        state = self.run_trace()
        self.assertEqual(set(state["transactions"]), {A, B, C, D})
        self.assertNotIn(A + ":1", state["outputs"])
        self.assertNotIn(X, state["transactions"])
        self.assertEqual(state["stats"]["requests_this_run"], 7)
        self.assertEqual(len(state["links"]), 4)

    def test_hop_zero_does_not_fetch_spends(self):
        state = self.run_trace(0)
        self.assertEqual(set(state["transactions"]), {A})
        self.assertEqual(state["stats"]["requests_this_run"], 1)
        self.assertEqual(state["outputs"][A + ":0"]["status"], "hop_limit")

    def test_hop_boundary_stops_and_preserves_all_branches(self):
        state = self.run_trace(1)
        self.assertEqual(set(state["transactions"]), {A, B})
        self.assertEqual(state["outputs"][B + ":0"]["status"], "hop_limit")
        self.assertEqual(state["outputs"][B + ":1"]["status"], "hop_limit")
        self.assertEqual(state["outputs"][B + ":2"]["status"], "fee")

    def test_continuation_matches_single_run_and_preserves_parent(self):
        parent = self.run_trace(1)
        snapshot = copy.deepcopy(parent)
        second = self.run_trace(3, parent)
        full = self.run_trace(3)
        self.assertEqual(parent, snapshot)
        self.assertEqual(set(second["links"]), set(full["links"]))
        self.assertEqual(second["stats"]["new_transactions_this_run"], 2)
        self.assertEqual(second["stats"]["requests_this_run"], 4)
        self.assertEqual(second["parent_run"], parent["run_id"])

    def test_merge_records_both_inputs_once(self):
        state = self.run_trace(2)
        self.assertEqual(state["links"][B + ":0"]["vin"], 0)
        self.assertEqual(state["links"][B + ":1"]["vin"], 1)
        self.assertEqual(state["stats"]["new_transactions_this_run"], 3)

    def multi_seed_fixture(self):
        txids = [hashlib.sha256(f"SYNTHETIC-start-{i}".encode()).hexdigest() for i in range(10)]
        joined = hashlib.sha256(b"SYNTHETIC-shared-spend").hexdigest()
        data, inputs = {}, []
        for index, txid in enumerate(txids):
            selected = output(f"SYNTHETIC-selected-{index}")
            data["/tx/" + txid] = {"txid": txid, "vin": [],
                "vout": [selected, output(f"SYNTHETIC-unselected-{index}")], "status": dict(CONFIRMED)}
            data["/tx/" + txid + "/outspends"] = [
                {"spent": True, "txid": joined, "vin": index, "status": dict(CONFIRMED)},
                {"spent": False}]
            inputs.append({"txid": txid, "vout": 0, "prevout": selected})
        data["/tx/" + joined] = {"txid": joined, "vin": inputs,
            "vout": [output("SYNTHETIC-shared-descendant")], "status": dict(CONFIRMED)}
        save_json(self.fixture_file, data)
        return txids, joined

    def test_ten_initial_transactions_share_one_run_and_one_descendant(self):
        txids, joined = self.multi_seed_fixture()
        limits = Limits(max_hops=1, max_transactions=11)
        api = Esplora(self.store, "pending", limits, fixture=self.fixture_file, min_interval=0)
        seeds = [txid + ":0" for txid in txids]
        state = new_state(seeds, api.base, limits, [])
        api.run_id = state["run_id"]
        state = trace(api, state, limits, self.root / "multi-seed.json")
        self.assertEqual(set(state["seeds"]), set(seeds))
        self.assertEqual(set(state["transactions"]), set(txids) | {joined})
        self.assertEqual(state["stats"]["new_transactions_this_run"], 11)
        self.assertEqual(len(state["links"]), 10)
        self.assertEqual({link["vin"] for link in state["links"].values()}, set(range(10)))
        self.assertTrue(all(txid + ":1" not in state["outputs"] for txid in txids))
        self.assertEqual(state["outputs"][joined + ":0"]["status"], "hop_limit")
        graph = build_graph(state)
        self.assertEqual(sum(node["id"] == "tx:" + joined for node in graph["nodes"]), 1)

    def test_multiple_seeds_share_transaction_budget_and_resume_remaining_roots(self):
        txids, joined = self.multi_seed_fixture()
        seeds = [txid + ":0" for txid in txids]
        limits = Limits(max_hops=1, max_transactions=4)
        api = Esplora(self.store, "pending", limits, fixture=self.fixture_file, min_interval=0)
        state = new_state(seeds, api.base, limits, [])
        api.run_id = state["run_id"]
        state = trace(api, state, limits, self.root / "multi-paused.json")
        self.assertEqual(state["stop_reason"], "transaction_limit")
        self.assertEqual(state["stats"]["new_transactions_this_run"], 4)
        self.assertEqual(set(state["seeds"]), set(seeds))
        snapshot = copy.deepcopy(state)
        continued_limits = Limits(max_hops=1, max_transactions=20)
        api = Esplora(self.store, "pending", continued_limits, fixture=self.fixture_file, min_interval=0)
        continued = new_state(seeds, api.base, continued_limits, [], parent=state)
        api.run_id = continued["run_id"]
        continued = trace(api, continued, continued_limits, self.root / "multi-continued.json")
        self.assertEqual(state, snapshot)
        self.assertEqual(continued["parent_run"], state["run_id"])
        self.assertEqual(set(continued["transactions"]), set(txids) | {joined})
        self.assertEqual(len(continued["links"]), 10)

    def test_request_budget_preserves_current_and_can_resume(self):
        state = self.run_trace(max_requests=2)
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["stats"]["requests_this_run"], 2)
        self.assertEqual(state["outputs"][A + ":0"]["status"], "request_limit")
        continued = self.run_trace(3, state)
        self.assertEqual(set(continued["transactions"]), {A, B, C, D})

    def test_transaction_and_outpoint_limits(self):
        state = self.run_trace(max_transactions=1)
        self.assertEqual(state["stop_reason"], "transaction_limit")
        self.assertEqual(len(state["transactions"]), 1)
        state = self.run_trace(max_outpoints=1)
        self.assertEqual(state["stop_reason"], "outpoint_limit")
        self.assertGreater(state["stats"]["frontier_count"], 0)

    def test_confidential_value_and_asset_are_not_invented(self):
        state = self.run_trace(1)
        output = state["transactions"][B]["data"]["vout"][0]
        self.assertNotIn("value", output)
        self.assertNotIn("asset", output)
        self.assertEqual(quantity(output), "amount confidential; asset unknown")
        self.assertIn("0 base units", quantity({"value": 0}))

    def test_pegout_and_unspendable_outputs_stop(self):
        state = self.run_trace(4)
        self.assertEqual(state["outputs"][D + ":0"]["status"], "pegout")
        self.assertEqual(state["outputs"][C + ":1"]["status"], "provably_unspendable")
        self.assertEqual(state["stats"]["frontier_count"], 0)

    def test_spend_reference_mismatch_is_error_and_kept(self):
        data = fixture()
        data["/tx/" + A + "/outspends"][0]["vin"] = 7
        save_json(self.fixture_file, data)
        state = self.run_trace()
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["outputs"][A + ":0"]["status"], "error")
        self.assertEqual(len(state["links"]), 0)

    def test_unspent_and_unconfirmed_are_frontier_not_completed_paths(self):
        for status, expected in (({"spent": False}, "unspent_at_observation"),
            ({"spent": True, "txid": B, "vin": 0, "status": {"confirmed": False}}, "unconfirmed_spend")):
            data = fixture()
            data["/tx/" + A + "/outspends"][0] = status
            save_json(self.fixture_file, data)
            state = self.run_trace()
            self.assertEqual(state["outputs"][A + ":0"]["status"], expected)
            self.assertEqual(state["stats"]["frontier_count"], 1)

    def test_address_label_stop_with_provenance(self):
        labels = [{"kind": "address", "value": "SYNTHETIC-branch-A", "entity": "Demo service",
            "source": "case-record:synthetic", "confidence": "candidate", "observed_at": "2026-09-09", "stop": True}]
        state = self.run_trace(labels=labels)
        self.assertEqual(state["outputs"][B + ":0"]["status"], "analyst_stop")
        self.assertEqual(state["outputs"][B + ":0"]["labels"][0]["source"], labels[0]["source"])

    def test_selective_resume_keeps_unselected_branch(self):
        parent = self.run_trace(1)
        state = self.run_trace(2, parent, only={B + ":0"})
        self.assertEqual(state["outputs"][B + ":1"]["status"], "hop_limit")
        self.assertNotIn(B + ":1", state["links"])

    def test_default_graph_keeps_reused_address_outpoints_separate(self):
        state = self.run_trace()
        graph = build_graph(state)
        repeated = [node for node in graph["nodes"] if node["details"].get("address") == "SYNTHETIC-branch-A"]
        self.assertEqual(len(repeated), 2)
        merged = build_graph(state, True)
        self.assertEqual(len([node for node in merged["nodes"] if node["details"].get("address") == "SYNTHETIC-branch-A"]), 1)
        ET.fromstring(svg_graph(graph))

    def test_evidence_export_and_integrity(self):
        state = self.run_trace()
        out = self.root / "export"
        export_run(self.store, state, out, offline_preview=True)
        self.assertTrue((out / "graph.html").exists())
        metadata = read_json(out / "evidence-index.json")
        self.assertEqual(len(metadata), 7)
        self.assertEqual(json.loads((out / metadata[0]["file"]).read_bytes())["txid"], A)
        with self.store.db:
            self.store.db.execute("UPDATE observations SET body=? WHERE id=?", (b"altered", metadata[0]["id"]))
        with self.assertRaises(TraceError):
            list(self.store.observations([metadata[0]["id"]]))


class ApiAndMiroTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "case")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_oauth_refresh_401_and_secret_redaction(self):
        calls = []
        def transport(method, url, headers, body, timeout):
            calls.append((method, headers))
            if method == "POST":
                self.assertIn(b"grant_type=client_credentials", body)
                return 200, {}, b'{"access_token":"private-token","expires_in":300}'
            if len(calls) == 2:
                return 401, {}, b'{}'
            return 200, {}, canonical(fixture()["/tx/" + A])
        with patch.dict(os.environ, {"BLOCKSTREAM_CLIENT_ID": "private-client", "BLOCKSTREAM_CLIENT_SECRET": "private-secret"}):
            api = Esplora(self.store, "test", Limits(), min_interval=0, transport=transport)
            data, _ = api.get("/tx/" + A)
        self.assertEqual(data["txid"], A)
        self.assertEqual(api.budget.requests, 4)
        archive = (self.root / "case" / "evidence.sqlite").read_bytes()
        self.assertNotIn(b"private-secret", archive)
        self.assertNotIn(b"private-token", archive)

    def test_oauth_counts_against_request_budget(self):
        def transport(*args):
            return 200, {}, b'{"access_token":"secret","expires_in":300}'
        with patch.dict(os.environ, {"BLOCKSTREAM_CLIENT_ID": "client", "BLOCKSTREAM_CLIENT_SECRET": "secret"}):
            api = Esplora(self.store, "test", Limits(max_requests=1), min_interval=0, transport=transport)
            with self.assertRaises(StopRun):
                api.get("/tx/" + A)
        self.assertEqual(api.budget.requests, 1)

    def test_credentials_cannot_be_sent_to_custom_host(self):
        with self.assertRaises(TraceError):
            Esplora(self.store, "test", Limits(), base="https://other.example/liquid/api")

    def test_elapsed_time_budget_stops_before_request(self):
        with patch("liquid_tracer.api.time.monotonic", side_effect=[10., 20.]):
            budget = Budget(Limits(max_seconds=5))
            with self.assertRaises(StopRun):
                budget.request()
        self.assertEqual(budget.requests, 0)

    def test_nonfinite_duration_is_rejected(self):
        with self.assertRaises(TraceError):
            Limits(max_seconds=float("nan")).validate()

    def test_spend_cache_refreshes_each_run(self):
        calls = []
        def transport(*args):
            calls.append(1)
            return 200, {}, b'[{"spent":false}]'
        for run in ("run1", "run2"):
            api = Esplora(self.store, run, Limits(), auth="none", min_interval=0, transport=transport)
            api.get("/tx/" + A + "/outspends")
            api.get("/tx/" + A + "/outspends")
        self.assertEqual(len(calls), 2)

    def sample_plan(self):
        return make_plan({"run_id": "test", "simulated": True, "notice": "synthetic", "nodes": [
            {"id": "a", "label": "Address", "kind": "address", "color": "#f5f6f8", "x": 0, "y": 0, "width": 160, "height": 160},
            {"id": "t", "label": "Transaction", "kind": "transaction", "color": "#a6ccf5", "x": 390, "y": 0, "width": 160, "height": 160}],
            "edges": [{"id": "e", "source": "a", "target": "t", "label": "vin 0", "quantity": "unknown", "role": "traced_input"}]})

    def test_miro_mapping_and_acknowledged_resume(self):
        plan, calls = self.sample_plan(), []
        def transport(method, url, headers, body, timeout):
            calls.append(json.loads(body))
            return 201, {}, canonical({"id": "remote-" + str(len(calls))})
        state_path = self.root / "miro.json"
        publish(plan, "board=", state_path, token="token", transport=transport, interval=0)
        self.assertEqual(calls[-1]["startItem"]["id"], "remote-2")
        self.assertEqual(calls[-1]["endItem"]["id"], "remote-3")
        publish(plan, "board=", state_path, token="token", transport=transport, interval=0)
        self.assertEqual(len(calls), 4)
        with self.assertRaises(TraceError):
            publish(plan, "different", state_path, token="token", transport=transport)

    def test_miro_ambiguous_post_requires_reconciliation(self):
        plan = self.sample_plan()
        state_path = self.root / "miro.json"
        def transport(*args):
            return 503, {}, b'{}'
        with self.assertRaises(TraceError):
            publish(plan, "board", state_path, token="token", transport=transport, interval=0)
        self.assertEqual(read_json(state_path)["pending"]["key"], "legend")
        with self.assertRaises(TraceError):
            publish(plan, "board", state_path, token="token", transport=transport, interval=0)
        resolve(state_path, item_id="already-created")
        self.assertEqual(read_json(state_path)["items"]["legend"], "already-created")

    def test_miro_item_limit_before_network(self):
        with self.assertRaises(TraceError):
            publish(self.sample_plan(), "board", self.root / "miro.json", max_items=1, token="token")


if __name__ == "__main__":
    unittest.main()
