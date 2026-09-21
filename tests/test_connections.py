"""Exact hop-bounded path unions, saved evidence, UI routes and safe publication."""
import copy
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, digest, read_json, save_json
from liquid_tracer.connections import (connection_graph, connecting_outpoints, preview_connections,
                                        reviewed_connections, publish_connections)
from liquid_tracer.export import build_graph
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.miro import make_plan, validate_plan, _namespace
from liquid_tracer.services import load_services, set_service
from liquid_tracer.name_colors import set_name_colors
from tests.test_attribution_convergence import graph_state, annotation, tx
from tests.test_branch_interactions import shared_state, receiving
from tests.fixtures import output
from tests.test_layout import state_from


def saved_case(case, state=None):
    state = copy.deepcopy(state or graph_state((("a:0", "c"), ("c:0", "b"))))
    state.update(case_id=read_case(case)["case_id"], run_id="0123456789abcdef", status="bounded_complete",
                 stop_reason=None, include_unconfirmed=False)
    state.setdefault("limits", {})["max_hops"] = 10
    directory = case / "runs" / state["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    graph = build_graph(state)
    for name, value in (("trace.json", state), ("graph.json", graph), ("miro-plan.json", make_plan(graph))):
        save_json(directory / name, value)
    (directory / "SHA256SUMS").write_text("".join(digest((directory / name).read_bytes()) + "  " + name + "\n"
                for name in ("trace.json", "graph.json", "miro-plan.json")))
    save_json(case / "case.json", {**read_case(case), "latest_run": state["run_id"]})
    return state, directory


class ConnectionPathTests(unittest.TestCase):
    def test_direct_and_exact_hop_boundaries(self):
        direct = graph_state((("a:0", "b"),))
        self.assertEqual(connecting_outpoints(direct, 0)["outpoints"], [])
        self.assertEqual(connecting_outpoints(direct, 1)["outpoints"], [tx("a") + ":0"])
        state = graph_state((("a:0", "c"), ("c:0", "b")))
        self.assertFalse(connecting_outpoints(state, 1)["pairs"])
        result = connecting_outpoints(state, 2)
        self.assertEqual(result["pairs"], [{"source": tx("a"), "target": tx("b"), "shortest_hops": 2}])
        self.assertEqual(set(result["outpoints"]), {tx("a") + ":0", tx("c") + ":0"})

    def test_alternate_paths_included_not_just_shortest_and_dead_end_excluded(self):
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("c:0", "b"), ("a:2", "d")),
                            seeds=("a:0", "a:1", "a:2", "b:0"))
        self.assertEqual(connecting_outpoints(state, 1)["outpoints"], [tx("a") + ":0"])
        self.assertEqual(set(connecting_outpoints(state, 2)["outpoints"]),
                         {tx("a") + ":0", tx("a") + ":1", tx("c") + ":0"})
        g = connection_graph(state, 2)
        self.assertNotIn("tx:" + tx("d"), {n["id"] for n in g["nodes"]})
        self.assertEqual(len(g["edges"]), 6)

    def test_shared_address_and_common_descendant_are_not_starter_paths(self):
        self.assertEqual(connection_graph(shared_state())["nodes"], [])
        state = graph_state((("a:0", "c"), ("b:0", "c")))
        self.assertEqual(connection_graph(state)["nodes"], [])

    def test_unconnected_starter_is_omitted_but_endpoints_remain(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0", "b:0", "c:0"))
        graph = connection_graph(state)
        self.assertEqual({n["id"] for n in graph["nodes"] if n["kind"] == "transaction"}, {"tx:" + tx("a"), "tx:" + tx("b")})
        self.assertTrue(all(n["role"] == "starting_transaction" for n in graph["nodes"] if n["kind"] == "transaction"))
        self.assertEqual(len(graph["nodes"]), 3)

    def test_hops_do_not_reset_at_intermediate_starter(self):
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0", "b:0", "c:0"))
        pairs = connecting_outpoints(state, 1)["pairs"]
        self.assertEqual(len(pairs), 2)
        self.assertFalse(any(p["source"] == tx("a") and p["target"] == tx("c") for p in pairs))
        self.assertEqual(len(connecting_outpoints(state, 2)["pairs"]), 3)

    def test_unselected_seed_sibling_does_not_start_a_path(self):
        state = graph_state((("a:1", "b"),))
        self.assertFalse(connecting_outpoints(state)["pairs"])

    def test_stop_boundary_removes_path_even_from_old_spent_evidence(self):
        state = graph_state((("a:0", "c"), ("c:0", "b")), labels=[annotation(stop=True, address="SYNTHETIC-c-address")])
        self.assertFalse(connecting_outpoints(state)["pairs"])
        state["labels"][0]["stop"] = False
        self.assertTrue(connecting_outpoints(state)["pairs"])

    def test_context_input_does_not_create_verified_path(self):
        state = graph_state(raw_links=(("a:0", "b"),))
        self.assertFalse(connecting_outpoints(state)["pairs"])

    def test_unconfirmed_requires_existing_trace_policy(self):
        state = graph_state((("a:0", "b"),))
        state["transactions"][tx("b")]["data"]["status"] = {"confirmed": False}
        self.assertFalse(connecting_outpoints(state)["pairs"])
        state["include_unconfirmed"] = True
        self.assertTrue(connecting_outpoints(state)["pairs"])

    def test_corrupt_inputs_pegins_and_self_spends_rejected(self):
        for mutation in (lambda s: s["links"][tx("a")+":0"].update(vin=3),
                         lambda s: s["transactions"][tx("b")]["data"]["vin"][0].update(is_pegin=True),
                         lambda s: s["transactions"][tx("b")]["data"]["vin"][0].update(vout=True),
                         lambda s: s["outputs"][tx("a")+":0"].update(vout=5)):
            state = graph_state((("a:0", "b"),)); mutation(state)
            with self.assertRaises(TraceError): connecting_outpoints(state)

    def test_cycle_rejected_not_walked_as_bounded_paths(self):
        state = graph_state((("a:0", "b"), ("b:0", "a")))
        with self.assertRaisesRegex(TraceError, "cycle"): connecting_outpoints(state)

    def test_invalid_hops_and_one_unique_starter_fail_before_graph(self):
        for hops in (True, -1, 1.5, "10", None, 2147483648):
            with self.assertRaises(TraceError): connecting_outpoints(graph_state(), hops)
        with self.assertRaises(TraceError): connecting_outpoints(graph_state(seeds=("a:0", "a:1")))

    def test_state_colors_labels_and_verified_outpoints_are_preserved(self):
        state = graph_state((("a:0", "c"), ("c:0", "b")))
        state["service_controls"] = {"role_colors": {"seed": "#112233", "starting_transaction": "#334455", "candidate": "#556677"}}
        before = copy.deepcopy(state)
        graph = connection_graph(state, 2)
        validate_plan(make_plan(graph))
        self.assertEqual(before, state)
        self.assertTrue(all(e["outpoint"] in graph["connections"]["outpoints"] for e in graph["edges"]))
        self.assertFalse(any(e["role"].startswith("context") for e in graph["edges"]))
        self.assertEqual({n["color"] for n in graph["nodes"] if n.get("role") == "seed"}, {"#112233"})
        self.assertEqual(graph["address_mode"], "outpoint_occurrences")

    def test_parallel_utxos_remain_separate_and_deterministic(self):
        state = graph_state((("a:0", "b"), ("a:1", "b")), seeds=("a:0", "a:1", "b:0"))
        before = connecting_outpoints(state)
        state["links"] = dict(reversed(list(state["links"].items())))
        state["seeds"].reverse()
        self.assertEqual(connecting_outpoints(state), before)
        graph = connection_graph(state)
        self.assertEqual(len([n for n in graph["nodes"] if n["kind"] == "address"]), 2)
        self.assertEqual(len(graph["edges"]), 4)

    def test_random_dags_match_exhaustive_path_union_oracle(self):
        rng = random.Random(842)
        for iteration in range(100):
            ids = [f"{n+1:064x}" for n in range(7)]
            txs = {key: {"txid": key, "vin": [], "vout": [], "status": {"confirmed": True, "block_time": i+1}} for i, key in enumerate(ids)}
            edges = []
            for i, parent in enumerate(ids):
                for child in ids[i+1:]:
                    if rng.random() < .4:
                        index = len(txs[parent]["vout"]); out = output("SYNTHETIC-"+str(i))
                        txs[parent]["vout"].append(out)
                        vin = len(txs[child]["vin"])
                        txs[child]["vin"].append({"txid": parent, "vout": index, "prevout": copy.deepcopy(out)})
                        edges.append((parent, child, f"{parent}:{index}", vin))
            for i, t in enumerate(txs.values()): t["vout"].append(output("SYNTHETIC-last-"+str(i)))
            state = state_from(txs)
            roots = {ids[i] for i in (0, 3, 6)}
            seeds = {f"{r}:{i}" for r in roots for i in range(len(txs[r]["vout"])) if i%2 == 0}
            state.update(seeds=sorted(seeds), labels=[], links={}, outputs={})
            for parent, child, key, vin in edges:
                state["links"][key] = {"spending_txid": child, "vin": vin}
                state["outputs"][key] = {"txid": parent, "vout": int(key.rpartition(":")[2])}
            hops = rng.randrange(5)
            expected = set()
            for root in roots:
                stack = [(root, [])]
                while stack:
                    current, path = stack.pop()
                    if current in roots and current != root: expected.update(path)
                    if len(path) >= hops: continue
                    for parent, child, key, _ in edges:
                        if parent == current and (current != root or key in seeds):
                            stack.append((child, path+[key]))
            self.assertEqual(set(connecting_outpoints(state, hops)["outpoints"]), expected, iteration)


class ConnectionPreviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.case = create_investigation(Path(tmp.name), "Connections")
        self.state, self.archive = saved_case(self.case)

    def test_preview_is_offline_complete_and_keeps_archive(self):
        before = {p.name: p.read_bytes() for p in self.archive.iterdir()}
        with patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("no API")):
            result = preview_connections(self.case, max_hops=2)
        graph, plan = reviewed_connections(self.case, result["preview_id"])
        self.assertEqual(result["connection_count"], 1)
        self.assertEqual(plan["schema_version"], 1)
        with self.assertRaises(TraceError): _namespace(plan)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.archive.iterdir()})
        self.assertEqual(len(graph["edges"]), 4)
        self.assertIn("does not fetch", result["notice"])
        directory = Path(result["directory"])
        self.assertIn("flowchart LR", (directory/"graph.mmd").read_text())
        self.assertIn("#c4b5fd", (directory/"graph.mmd").read_text())

    def test_no_matches_create_empty_graph_and_no_publication_or_layout(self):
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("no layout")):
            result = preview_connections(self.case, max_hops=1)
        graph, _ = reviewed_connections(self.case, result["preview_id"])
        self.assertEqual(graph["nodes"], []); self.assertEqual(graph["edges"], [])
        with patch("liquid_tracer.miro.publish", side_effect=AssertionError("no board writes")):
            self.assertEqual(publish_connections(self.case, result["preview_id"], "unused")["items"], 0)
        self.assertFalse((self.case/"miro").exists())

    def test_current_arrow_preference_colors_connections_and_invalidates_old_preview(self):
        set_service(self.case, "SYNTHETIC-c-address", name="Exchange", stop_tracing=False)
        set_name_colors(self.case, [{"name": "Exchange", "color": "#123456"}],
                        expected_revision=load_services(self.case)["revision"])
        original = preview_connections(self.case, max_hops=2)
        self.assertFalse(original["color_attribution_arrows"])
        update_case(self.case, {"run_defaults": {"color_attribution_arrows": True}})
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_connections(self.case, original["preview_id"])
        result = preview_connections(self.case, max_hops=2)
        graph, _ = reviewed_connections(self.case, result["preview_id"])
        self.assertTrue(result["color_attribution_arrows"])
        self.assertTrue(graph["graph_options"]["color_attribution_arrows"])
        named = {node["id"] for node in graph["nodes"] if node.get("color_source") == "name"}
        self.assertTrue(named)
        attached = [edge for edge in graph["edges"] if {edge["source"], edge["target"]} & named]
        self.assertEqual(len(attached), 2)
        self.assertEqual({edge["color"] for edge in attached}, {"#123456"})

    def test_stale_settings_and_modified_previews_fail_closed(self):
        result = preview_connections(self.case, max_hops=2)
        set_service(self.case, "SYNTHETIC-c-address", name="Stop", stop_tracing=True)
        with self.assertRaisesRegex(TraceError, "changed"): reviewed_connections(self.case, result["preview_id"])
        result = preview_connections(self.case, max_hops=2)
        (Path(result["directory"])/"graph.svg").write_text("changed")
        with self.assertRaisesRegex(TraceError, "changed"): reviewed_connections(self.case, result["preview_id"])

    def test_full_trace_board_is_protected(self):
        result = preview_connections(self.case, max_hops=2)
        update_case(self.case, {"miro_board": "full-board"})
        with patch("liquid_tracer.miro.publish", side_effect=AssertionError("no writes")):
            with self.assertRaisesRegex(TraceError, "separate"): publish_connections(self.case, result["preview_id"], "full-board")

    def test_wrong_preview_paths_and_case_rejected(self):
        for identity in (None, "../../private", "a"*16+"-elk-12345678"):
            with self.assertRaises(TraceError): reviewed_connections(self.case, identity)

    def test_publication_reuses_existing_snapshot_items(self):
        from tests.test_miro_sync import FakeMiro
        result = preview_connections(self.case, max_hops=2)
        remote = FakeMiro()
        first = publish_connections(self.case, result["preview_id"], "snapshot-board", token="test", transport=remote, interval=0)
        second = publish_connections(self.case, result["preview_id"], "snapshot-board", token="test", transport=remote, interval=0)
        self.assertEqual(first["items"], second["items"])
        next_preview = preview_connections(self.case, max_hops=3)
        with self.assertRaisesRegex(TraceError, "different board or plan"):
            publish_connections(self.case, next_preview["preview_id"], "snapshot-board", token="test", transport=remote, interval=0)
