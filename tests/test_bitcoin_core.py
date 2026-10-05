"""Bitcoin uses the same exact-outpoint graph pipeline without Liquid assumptions."""
import copy
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import TraceError, canonical, digest, output_kind, save_json
from liquid_tracer.export import build_graph, graph_quantity, html_graph, legend_lines, svg_graph
from liquid_tracer.address_activity import validate_address
from liquid_tracer.inspection import inspect_transaction, transaction_outputs
from liquid_tracer.networks import blockchain, default_api, explorer_root, is_primary
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.fixtures import A, B, C, D, X, CONFIRMED


def bitcoin_fixture():
    def output(address, value, script=None):
        result = {"scriptpubkey": "0014" + "aa" * 20 if script is None else script,
                  "scriptpubkey_type": "v0_p2wpkh", "value": value}
        if address:
            result["scriptpubkey_address"] = address
        return result
    a = output("bc1qseed", 200_000_000)
    dormant = output("bc1qdormant", 50_000_000)
    service = output("bc1qservice", 75_000_000)
    empty = output(None, 74_999_000, "")
    op_return = {"scriptpubkey": "6a0101", "scriptpubkey_type": "op_return", "value": 0}
    def tx(txid, inputs, outputs):
        return {"txid": txid, "status": dict(CONFIRMED), "vin": inputs, "vout": outputs, "fee": 1000}
    def vin(txid, index, prevout):
        return {"txid": txid, "vout": index, "prevout": prevout}
    def spent(txid, index=0):
        return {"spent": True, "txid": txid, "vin": index, "status": dict(CONFIRMED)}
    return {
        "/tx/" + A: tx(A, [], [a, output("bc1qunselected", 10_000)]),
        "/tx/" + A + "/outspends": [spent(B), spent(X)],
        "/tx/" + B: tx(B, [vin(A, 0, a), vin(X, 0, output("bc1qcontextone", 1_000)),
                            vin(X, 1, output("bc1qcontexttwo", 1_000))],
                       [dormant, service, op_return, empty]),
        "/tx/" + B + "/outspends": [{"spent": False}, spent(D), {"spent": False}, spent(C)],
        "/tx/" + C: tx(C, [vin(B, 3, empty)], [output("bc1qfinal", 74_998_000)]),
        "/tx/" + C + "/outspends": [{"spent": False}],
    }


def bitcoin_graph_state(*args, **kwargs):
    from tests.test_attribution_convergence import graph_state
    state = graph_state(*args, **kwargs)
    state.update(blockchain="bitcoin", source="https://blockstream.info/api")
    for record in state["transactions"].values():
        for output in record["data"]["vout"]:
            for field in ("asset", "assetcommitment", "valuecommitment"):
                output.pop(field, None)
            output["value"] = 123456789
    for record in state["transactions"].values():
        for vin in record["data"]["vin"]:
            vin["prevout"] = copy.deepcopy(state["transactions"][vin["txid"]]["data"]["vout"][vin["vout"]])
    return state


class BitcoinCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / "fixture.json"
        self.data = bitcoin_fixture()
        save_json(self.fixture, self.data)
        self.store = Store(self.root / "case")
        self.addCleanup(self.store.close)

    def traced(self):
        limits = Limits(max_hops=3)
        labels = [{"kind": "address", "value": "bc1qservice", "entity": "Coinbase",
                   "stop": True, "confidence": "confirmed", "source": "Investigator supplied TRM attribution",
                   "observed_at": "2026-10-05"}]
        with Esplora(self.store, "pending", limits, fixture=self.fixture, blockchain="bitcoin") as api:
            state = new_state([A + ":0"], api.base, limits, labels, blockchain="bitcoin")
            api.run_id = state["run_id"]
            return trace(api, state, limits, self.root / "trace.json")

    def test_network_identity_and_explicit_endpoint_validation(self):
        self.assertEqual(blockchain({}), "liquid")
        self.assertEqual(blockchain({"blockchain": "bitcoin"}), "bitcoin")
        for value in ({"blockchain": None}, "ethereum", True):
            with self.assertRaises(TraceError):
                blockchain(value)
        with Esplora(self.store, "btc", Limits(), blockchain="bitcoin") as api:
            self.assertEqual(api.base, default_api("bitcoin"))
        for chain, base in (("bitcoin", default_api("liquid")), ("liquid", default_api("bitcoin"))):
            with self.assertRaises(TraceError):
                Esplora(self.store, "wrong", Limits(), base=base, blockchain=chain)
        with self.assertRaises(TraceError):
            Esplora(self.store, "creds", Limits(), base="https://blockstream.info/api", blockchain="bitcoin")
        self.assertEqual(explorer_root("bitcoin"), "https://blockstream.info")
        self.assertEqual(explorer_root("bitcoin", source="https://blockstream.info/testnet/api"),
                         "https://blockstream.info/testnet")
        self.assertEqual(explorer_root("liquid", source="https://blockstream.info/liquidtestnet/api"),
                         "https://blockstream.info/liquidtestnet")

    def test_bitcoin_trace_keeps_exact_links_stops_and_empty_script(self):
        state = self.traced()
        self.assertEqual(state["blockchain"], "bitcoin")
        self.assertEqual(set(state["transactions"]), {A, B, C})
        self.assertNotIn(A + ":1", state["outputs"])
        self.assertEqual(state["outputs"][B + ":0"]["status"], "unspent_at_observation")
        self.assertEqual(state["outputs"][B + ":1"]["status"], "suspected_service_stop")
        self.assertEqual(state["outputs"][B + ":2"]["status"], "provably_unspendable")
        self.assertEqual(state["links"][B + ":3"]["spending_txid"], C)
        self.assertTrue(all(record["data"] == self.data["/tx/" + txid]
                            for txid, record in state["transactions"].items()))
        with self.assertRaisesRegex(TraceError, "same blockchain"):
            new_state([], state["source"], Limits(), [], parent=state, blockchain="liquid")

    def test_bitcoin_graph_and_csv_preserve_known_values_and_attribution(self):
        state = self.traced()
        state["service_controls"] = {"name_colors": {"coinbase": "#123456"}}
        before = canonical(state)
        graph = build_graph(state, group_context_inputs=True, initial_layout=False)
        self.assertEqual(graph["blockchain"], "bitcoin")
        self.assertTrue(all(is_primary(n, graph) for n in graph["nodes"] if n["kind"] == "address"))
        self.assertEqual(graph["context_groups"]["group_count"], 1)
        service = next(n for n in graph["nodes"] if n["id"] == "bitcoin:address:bc1qservice")
        self.assertIn("Coinbase", service["label"])
        self.assertIn("STOP TRACING", service["label"])
        self.assertEqual(service["color"], "#123456")
        self.assertEqual(graph["fee_items"], {})
        self.assertEqual(next(e for e in graph["edges"] if e["id"] == "out:" + A + ":0")["quantity"], "2 BTC")
        rows = transaction_csv_rows(graph, state)
        self.assertTrue(all(r["Asset"] == "BTC" for r in rows))
        service_row = next(r for r in rows if r["Transaction Hash"] == B and r["Direction"] == "OUT" and r["Number of I/O"] == 1)
        self.assertEqual(service_row["Asset Value"], 75_000_000)
        self.assertEqual(service_row["Address Label"], "Coinbase")
        self.assertEqual(canonical(state), before)
        self.assertIn("satoshis", " ".join(legend_lines(graph)))
        self.assertNotIn("L-BTC", " ".join(legend_lines(graph)))

    def test_bitcoin_links_use_bitcoin_explorer(self):
        state = self.traced()
        state["source"] = default_api("bitcoin")
        graph = build_graph(state, initial_layout=False)
        node = next(n for n in graph["nodes"] if n["id"] == "bitcoin:address:bc1qseed")
        self.assertEqual(node["url"], "https://blockstream.info/address/bc1qseed")
        tx = next(n for n in graph["nodes"] if n["id"] == "tx:" + A)
        self.assertEqual(tx["url"], "https://blockstream.info/tx/" + A)

    def test_liquid_pegin_context_stays_external_and_unattributed(self):
        from tests.test_transaction_csv import state_fixture
        state = state_fixture()
        vin = state["transactions"][A]["data"]["vin"][0]
        vin["is_pegin"] = True
        vin["prevout"] = {"scriptpubkey": "0014" + "bb" * 20,
                          "scriptpubkey_address": "bc1qexternal", "value": 123}
        state["labels"].append({"kind": "address", "value": "bc1qexternal", "entity": "Must not apply",
                                "stop": True, "confidence": "confirmed", "source": "test", "observed_at": "2026-10-05"})
        graph = build_graph(state, initial_layout=False)
        external = next(n for n in graph["nodes"] if n["id"] == "bitcoin:address:bc1qexternal")
        self.assertFalse(is_primary(external, graph))
        self.assertNotIn("Must not apply", external["label"])
        self.assertIsNone(external["url"])
        self.assertEqual(external["details"]["occurrences"][0]["labels"], [])
        rows = transaction_csv_rows(graph, state)
        incoming = next(r for r in rows if r["Transaction Hash"] == A and r["Direction"] == "IN")
        self.assertEqual(incoming["Asset"], "BTC")
        self.assertEqual(incoming["Address Label"], "")

    def test_bitcoin_bech32_case_normalizes_without_changing_base58(self):
        self.assertEqual(validate_address("BC1Q" + "A" * 30), "bc1q" + "a" * 30)
        self.assertEqual(validate_address("TB1Q" + "A" * 30), "tb1q" + "a" * 30)
        address = "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"
        self.assertEqual(validate_address(address), address)

    def test_bitcoin_inspection_and_classification(self):
        empty = self.data["/tx/" + B]["vout"][3]
        self.assertEqual(output_kind(empty, "bitcoin"), "spendable")
        self.assertEqual(output_kind(empty), "fee")
        report = transaction_outputs(B, self.data["/tx/" + B], blockchain="bitcoin")
        self.assertTrue(report["outputs"][3]["selectable"])
        self.assertFalse(report["outputs"][2]["selectable"])
        self.assertEqual(inspect_transaction(B, fixture=self.fixture, blockchain="bitcoin"), report)
        self.assertEqual(graph_quantity({"value": 1}, "bitcoin"), "0.00000001 BTC")
        self.assertEqual(graph_quantity({"value": 2 ** 53 + 1}, "bitcoin"), "90071992.54740993 BTC")
        self.assertEqual(graph_quantity({"value": 1}), "1 base units ??")

    def test_bitcoin_shortest_connections_preserve_each_starter_and_exact_paths(self):
        from liquid_tracer.connections import connection_graph
        from tests.test_attribution_convergence import annotation, tx
        state = bitcoin_graph_state(
            raw_links=(("a:0", "b"), ("b:0", "d"),
                       ("a:1", "e"), ("e:0", "f"), ("f:0", "d")),
            seeds=("a:0", "a:1", "b:0", "c:0", "d:0"),
            labels=[annotation(name="Starting service", stop=False)])
        before = copy.deepcopy(state)
        graph = connection_graph(state, None, connection_scope="shortest", transaction_io="complete")
        report = graph["connections"]
        self.assertEqual(report["pairs"], [
            {"source": tx("a"), "target": tx("b"), "shortest_hops": 1},
            {"source": tx("a"), "target": tx("d"), "shortest_hops": 2},
            {"source": tx("b"), "target": tx("d"), "shortest_hops": 1}])
        self.assertEqual(report["outpoints"], [tx("a") + ":0", tx("b") + ":0"])
        self.assertEqual(report["unconnected_starting_transactions"], [tx("c")])
        self.assertEqual({n["id"] for n in graph["nodes"] if n["kind"] == "transaction"},
                         {"tx:" + tx(name) for name in "abcd"})
        self.assertTrue(report["includes_all_starters"])
        self.assertEqual(graph["blockchain"], "bitcoin")
        address = next(n for n in graph["nodes"] if n["id"] == "bitcoin:address:SYNTHETIC-a-address")
        self.assertIn("Starting service", address["label"])
        self.assertEqual(address["url"], "https://blockstream.info/address/SYNTHETIC-a-address")
        self.assertTrue(all(edge["quantity"] == "1.23456789 BTC" for edge in graph["edges"]))
        self.assertEqual(state, before)

    def test_bitcoin_shared_receipts_keep_convergence_in_miro_and_rendering(self):
        from liquid_tracer.mermaid import _preview_html, mermaid_source
        from liquid_tracer.miro import make_plan, validate_plan
        from tests.test_branch_interactions import receiving, SHARED
        state = bitcoin_graph_state()
        receiving(state, "a:0")
        receiving(state, "b:0")
        graph = build_graph(state)
        address = next(n for n in graph["nodes"] if n["id"] == "bitcoin:address:" + SHARED)
        self.assertEqual(address["address_convergence"]["receipt_count"], 2)
        self.assertEqual(address["interaction_types"], ["shared_address_receipts"])
        self.assertEqual(address["address_convergence"]["address_key"], "bitcoin:address:" + SHARED)
        plan = make_plan(graph)
        validate_plan(plan)
        self.assertEqual(plan["blockchain"], "bitcoin")
        self.assertEqual(plan["address_convergences"], graph["address_convergences"])
        self.assertEqual(plan["activity_frames"]["blockchain"], "bitcoin")
        self.assertIn("Bitcoin UTXO trace", plan["activity_frames"]["outer"]["title"])
        self.assertIn("Bitcoin UTXO trace", plan["shapes"][0]["body"]["data"]["content"])
        svg = svg_graph(graph)
        self.assertIn("Bitcoin UTXO trace", svg)
        self.assertIn("<title>Bitcoin UTXO trace</title>", html_graph(graph, svg))
        self.assertIn("Local Bitcoin Network trace", mermaid_source(graph))
        self.assertIn("<title>Bitcoin trace", _preview_html(graph, svg.encode()))
        invalid = copy.deepcopy(plan)
        invalid["blockchain"] = "liquid"
        invalid["sha256"] = digest(canonical({k: v for k, v in invalid.items() if k != "sha256"}))
        with self.assertRaisesRegex(TraceError, "blockchain disagrees"):
            validate_plan(invalid)

    def test_legacy_liquid_miro_plans_retain_implicit_chain_and_frame_identity(self):
        from liquid_tracer.miro import make_plan, validate_plan
        from tests.test_attribution_convergence import graph_state
        graph = build_graph(graph_state())
        explicit = make_plan(graph)
        graph.pop("blockchain")
        legacy = make_plan(graph)
        self.assertNotIn("blockchain", legacy)
        self.assertNotIn("blockchain", legacy["activity_frames"])
        self.assertEqual(legacy["activity_frames"], explicit["activity_frames"])
        self.assertIn("Liquid UTXO trace", legacy["activity_frames"]["outer"]["title"])
        validate_plan(legacy)


if __name__ == "__main__":
    unittest.main()
