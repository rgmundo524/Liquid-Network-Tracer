"""Trace hubs follow their exact seeded entry while preserving real returns."""
import copy
import unittest

from liquid_tracer.common import LBTC, TraceError
from liquid_tracer.elk_layout import (_apply_candidate, _request_graph, attachment_point,
                                     layout_metrics, optimize_graph)
from liquid_tracer.export import build_graph
from liquid_tracer.hub_layout import hub_layout_view, hub_plan
from liquid_tracer.trace_layout import trace_order, trace_structure
from tests.fixtures import output
from tests.test_elk_layout import HAS_ELK, synthetic_candidate
from tests.test_hub_layout import node_id, transaction
from tests.test_layout import state_from, txid


HUB_A = "G" + "a" * 33
HUB_B = "H" + "b" * 33
DORMANT = "V" + "d" * 33


def pegout():
    return {"scriptpubkey": "6a", "scriptpubkey_type": "op_return", "value": 50,
            "asset": LBTC, "pegout": {"genesis_hash": "00" * 32,
            "scriptpubkey": "0014" + "bb" * 20,
            "scriptpubkey_address": "SYNTHETIC-bitcoin-request"}}


def terminal(name, parent, index, hub, kind="pegout"):
    value = transaction(name, [(parent, index, hub)], [DORMANT])
    if kind == "pegout":
        value["vout"] = [pegout()]
    elif kind == "unspendable":
        value["vout"] = [{"scriptpubkey": "6a", "scriptpubkey_type": "op_return",
                           "value": 0, "asset": LBTC}]
    return value


def flow_graph(values, seeds, hubs=(HUB_A,), unspent=()):
    state = state_from({value["txid"]: value for value in values})
    state["seeds"] = [txid(name) + ":" + str(index) for name, index in seeds]
    for key, record in state["transactions"].items():
        for index, vin in enumerate(record["data"]["vin"]):
            outpoint = vin["txid"] + ":" + str(vin["vout"])
            state["links"][outpoint] = {"spending_txid": key, "vin": index}
        for index, _ in enumerate(record["data"]["vout"]):
            outpoint = key + ":" + str(index)
            state["outputs"][outpoint] = {"outpoint": outpoint, "txid": key,
                                           "vout": index, "depth": 0, "status": "hop_limit"}
    for key in state["links"]:
        if key in state["outputs"]:
            state["outputs"][key]["status"] = "spent"
    for name, index in unspent:
        state["outputs"][txid(name) + ":" + str(index)].update(
            status="unspent_at_observation", observed_spend={"spent": False}, spend_observation_id=1)
    graph = build_graph(state, hub_addresses=list(hubs))
    graph["graph_options"]["layout_style"] = "trace"
    return graph


def mixed_terminal_graph():
    return flow_graph([
        transaction("start", [("outside", 0, "SYNTHETIC-outside")], [HUB_A] * 3),
        terminal("pegout", "start", 0, HUB_A),
        terminal("opreturn", "start", 1, HUB_A, "unspendable"),
        terminal("unspent", "start", 2, HUB_A, "unspent"),
    ], [("start", 0), ("start", 1), ("start", 2)], unspent=[("unspent", 0)])


def return_graph():
    returning = transaction("return", [("start", 0, HUB_A)], [HUB_A])
    returning["vout"].append(pegout())
    return flow_graph([
        transaction("start", [("outside", 0, "SYNTHETIC-outside")], [HUB_A]),
        returning,
        terminal("finish", "return", 0, HUB_A),
    ], [("start", 0)])


def hub_id(address):
    return "liquid:address:" + address


class TraceHubPlanTests(unittest.TestCase):
    def assert_order(self, plan, *keys):
        columns = [plan["columns"][key] for key in keys]
        self.assertTrue(all(left < right for left, right in zip(columns, columns[1:])), columns)

    def test_seeded_deposit_precedes_hub_and_all_three_terminal_kinds(self):
        graph = mixed_terminal_graph()
        original = copy.deepcopy(graph)
        plan = hub_plan(graph)
        self.assertEqual(plan["entries"], {hub_id(HUB_A): node_id("start")})
        endings = {node_id(name) for name in ("pegout", "opreturn", "unspent")}
        self.assertEqual(set(plan["forward_roots"][hub_id(HUB_A)]), endings)
        self.assertEqual(len({plan["columns"][key] for key in endings}), 1)
        for key in endings:
            self.assert_order(plan, node_id("start"), hub_id(HUB_A), key)
        rows = trace_structure(graph)["terminal_rows"]
        self.assertTrue(any(set(row["transactions"]) == endings for row in rows))
        view = hub_layout_view(graph)
        self.assertEqual(view["edges"], graph["edges"])
        self.assertEqual([n["details"] for n in view["nodes"]], [n["details"] for n in graph["nodes"]])
        self.assertEqual(graph, original)

    def test_trace_order_places_hub_after_entry_and_keeps_terminal_group_together(self):
        graph = mixed_terminal_graph()
        before = copy.deepcopy(graph)
        order = trace_order(graph)
        self.assertEqual(order[order.index(node_id("start")) + 1], hub_id(HUB_A))
        self.assertEqual(len(order), len(graph["nodes"]))
        self.assertEqual(set(order), {node["id"] for node in graph["nodes"]})
        structure = trace_structure(graph)
        group = structure["terminal_rows"][0]
        members = set().union(*(structure["branches"][index] for index in group["components"]))
        positions = [order.index(key) for key in members]
        self.assertEqual(set(order[min(positions):max(positions) + 1]), members)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(trace_order(graph), order)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(graph, before)
        graph["graph_options"]["layout_style"] = "standard"
        self.assertIsNone(trace_order(graph))

    def test_trace_order_places_parallel_hubs_in_one_block_after_their_entry(self):
        graph = flow_graph([
            transaction("start", [("outside", 0, "SYNTHETIC-outside")], [HUB_A, HUB_B]),
            terminal("finish-a", "start", 0, HUB_A),
            terminal("finish-b", "start", 1, HUB_B),
        ], [("start", 0), ("start", 1)], hubs=(HUB_A, HUB_B))
        order = trace_order(graph)
        start = order.index(node_id("start")) + 1
        self.assertEqual(order[start:start + 2], sorted((hub_id(HUB_A), hub_id(HUB_B))))
        self.assertEqual(len(order), len(set(order)))
        self.assertEqual(set(order), {node["id"] for node in graph["nodes"]})

    def test_same_hub_return_retains_forward_entry_and_every_original_connection(self):
        graph = return_graph()
        before = copy.deepcopy(graph)
        plan = hub_plan(graph)
        self.assertEqual(plan["entries"], {hub_id(HUB_A): node_id("start")})
        for name in ("return", "finish"):
            self.assert_order(plan, node_id("start"), hub_id(HUB_A), node_id(name))
        self.assertEqual(set(plan["forward_roots"][hub_id(HUB_A)]), {node_id("return"), node_id("finish")})
        self.assertEqual(graph, before)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(hub_plan(graph), plan)

    def test_sequential_hubs_keep_their_distinct_flow_stages(self):
        graph = flow_graph([
            transaction("start", [("outside", 0, "SYNTHETIC-outside")], [HUB_A]),
            transaction("middle", [("start", 0, HUB_A)], [HUB_B, HUB_B]),
            terminal("finish-a", "middle", 0, HUB_B),
            terminal("finish-b", "middle", 1, HUB_B),
        ], [("start", 0)], hubs=(HUB_A, HUB_B))
        before = copy.deepcopy(graph)
        plan = hub_plan(graph)
        self.assertEqual(plan["entries"], {hub_id(HUB_A): node_id("start"), hub_id(HUB_B): node_id("middle")})
        for name in ("finish-a", "finish-b"):
            self.assert_order(plan, node_id("start"), hub_id(HUB_A), node_id("middle"), hub_id(HUB_B), node_id(name))
        self.assertEqual(graph, before)

    def test_parallel_hubs_follow_their_common_entry_without_ordering_each_other(self):
        graph = flow_graph([
            transaction("start", [("outside", 0, "SYNTHETIC-outside")], [HUB_A, HUB_B]),
            terminal("finish-a", "start", 0, HUB_A),
            terminal("finish-b", "start", 1, HUB_B),
        ], [("start", 0), ("start", 1)], hubs=(HUB_A, HUB_B))
        plan = hub_plan(graph)
        self.assertEqual(plan["entries"], {hub_id(HUB_A): node_id("start"), hub_id(HUB_B): node_id("start")})
        for hub, child in ((HUB_A, "finish-a"), (HUB_B, "finish-b")):
            self.assert_order(plan, node_id("start"), hub_id(hub), node_id(child))
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(hub_plan(graph), plan)

    def test_return_from_second_hub_does_not_replace_the_first_hubs_seed_entry(self):
        graph = flow_graph([
            transaction("start", [("outside", 0, "SYNTHETIC-outside")], [HUB_A]),
            transaction("middle", [("start", 0, HUB_A)], [HUB_B]),
            transaction("return", [("middle", 0, HUB_B)], [HUB_A]),
            terminal("finish", "return", 0, HUB_A),
        ], [("start", 0)], hubs=(HUB_A, HUB_B))
        before = copy.deepcopy(graph)
        plan = hub_plan(graph)
        self.assertEqual(plan["entries"], {hub_id(HUB_A): node_id("start"), hub_id(HUB_B): node_id("middle")})
        self.assert_order(plan, node_id("start"), hub_id(HUB_A), node_id("middle"), hub_id(HUB_B), node_id("return"))
        self.assert_order(plan, hub_id(HUB_A), node_id("finish"))
        self.assertEqual(graph, before)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(hub_plan(graph), plan)

    def test_non_hub_coinput_still_delays_the_spender(self):
        graph = flow_graph([
            transaction("start", [("outside", 0, "SYNTHETIC-outside")], [HUB_A, "SYNTHETIC-ordinary"]),
            transaction("other", [("start", 1, "SYNTHETIC-ordinary")], ["SYNTHETIC-later"]),
            transaction("joined", [("start", 0, HUB_A), ("other", 0, "SYNTHETIC-later")], [DORMANT]),
        ], [("start", 0)])
        plan = hub_plan(graph)
        self.assert_order(plan, node_id("start"), hub_id(HUB_A), node_id("joined"))
        self.assert_order(plan, node_id("start"), node_id("other"), node_id("joined"))
        self.assertNotIn((node_id("joined"), 1), plan["cut_inputs"])

    def test_earlier_context_spender_is_not_forced_after_the_seeded_entry(self):
        graph = flow_graph([
            transaction("earlier", [("outside", 0, HUB_A)], ["SYNTHETIC-funding"]),
            transaction("start", [("earlier", 0, "SYNTHETIC-funding")], [HUB_A]),
            terminal("finish", "start", 0, HUB_A),
        ], [("start", 0)])
        plan = hub_plan(graph)
        self.assertEqual(plan["entries"][hub_id(HUB_A)], node_id("start"))
        self.assertEqual(plan["forward_roots"][hub_id(HUB_A)], [node_id("finish")])
        self.assert_order(plan, node_id("earlier"), node_id("start"), hub_id(HUB_A), node_id("finish"))

    def test_external_hub_keeps_its_existing_entry_placement(self):
        graph = flow_graph([terminal("start", "outside", 0, HUB_A)], [("start", 0)])
        plan = hub_plan(graph)
        self.assertFalse(plan.get("entries"))
        self.assertEqual(plan.get("forward_roots", plan["roots"])[hub_id(HUB_A)], [node_id("start")])
        self.assert_order(plan, hub_id(HUB_A), node_id("start"))
        graph["graph_options"]["layout_style"] = "standard"
        legacy = hub_plan(graph)
        self.assertEqual(plan["columns"], legacy["columns"])
        self.assertNotIn("entries", legacy)

    def test_matching_address_with_unrelated_outpoint_does_not_anchor_a_hub(self):
        shared = "SYNTHETIC-shared-context"
        graph = flow_graph([
            transaction("start", [("outside", 0, "SYNTHETIC-outside")], [shared]),
            transaction("middle", [("unrelated", 0, shared)], [HUB_A]),
            terminal("finish", "middle", 0, HUB_A),
        ], [("start", 0)])
        self.assertNotIn(hub_id(HUB_A), hub_plan(graph).get("entries", {}))

    def test_ambiguous_displayed_outpoint_cannot_establish_seed_reachability(self):
        shared = "SYNTHETIC-shared-context"
        original = flow_graph([
            transaction("start", [("outside", 0, "SYNTHETIC-outside")], [shared]),
            transaction("middle", [("start", 0, shared)], [HUB_A]),
            terminal("finish", "middle", 0, HUB_A),
        ], [("start", 0)])
        self.assertEqual(hub_plan(original)["entries"].get(hub_id(HUB_A)), node_id("middle"))
        for direction, name in (("in", "middle"), ("out", "start")):
            with self.subTest(direction=direction):
                graph = copy.deepcopy(original)
                key = direction + ":" + txid(name) + ":0"
                duplicate = copy.deepcopy(next(edge for edge in graph["edges"] if edge["id"] == key))
                duplicate["id"] += "-ambiguous"
                graph["edges"].append(duplicate)
                self.assertNotIn(hub_id(HUB_A), hub_plan(graph).get("entries", {}))

    def test_distinct_spenders_of_one_outpoint_do_not_anchor_either_downstream_hub(self):
        shared = "SYNTHETIC-shared-context"
        graph = flow_graph([
            transaction("start", [("outside", 0, "SYNTHETIC-outside")], [shared]),
            transaction("conflict-a", [("start", 0, shared)], [HUB_A]),
            transaction("conflict-b", [("start", 0, shared)], [HUB_B]),
            terminal("finish-a", "conflict-a", 0, HUB_A),
            terminal("finish-b", "conflict-b", 0, HUB_B),
        ], [("start", 0)], hubs=(HUB_A, HUB_B))
        before = copy.deepcopy(graph)
        plan = hub_plan(graph)
        self.assertFalse(plan.get("entries"))
        self.assertEqual(graph, before)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(hub_plan(graph), plan)

    def test_deep_trace_hub_walk_is_iterative_and_preserves_first_seed_entry(self):
        values = [transaction(0, [("outside", 0, "SYNTHETIC-outside")], [HUB_A])]
        values.extend(transaction(index, [(index - 1, 0, HUB_A)], [HUB_A]) for index in range(1, 1200))
        graph = flow_graph(values, [(0, 0)])
        original_columns = {node["id"]: node["column"] for node in graph["nodes"]}
        plan = hub_plan(graph)
        self.assertEqual(plan["entries"], {hub_id(HUB_A): node_id(0)})
        self.assertEqual(len(plan["forward_roots"][hub_id(HUB_A)]), 1199)
        self.assert_order(plan, node_id(0), hub_id(HUB_A), node_id(1199))
        self.assertEqual({node["id"]: node["column"] for node in graph["nodes"]}, original_columns)

    def test_candidate_cannot_place_seed_entry_after_its_hub(self):
        graph = mixed_terminal_graph()
        request, ports, fees = _request_graph(graph)
        request["children"].sort(key=lambda item: (int(item["layoutOptions"]["elk.partitioning.partition"]), item["id"]))
        candidate = synthetic_candidate(request, [1])[0]
        _apply_candidate(graph, candidate, ports, fees, "elbowed")
        by_id = {node["id"]: node for node in candidate["nodes"]}
        by_id[node_id("start")]["x"] = by_id[hub_id(HUB_A)]["x"] + 500
        with self.assertRaisesRegex(TraceError, "forward entry into a hub"):
            _apply_candidate(graph, candidate, ports, fees, "elbowed")

    def test_anchored_hub_returns_and_spends_use_different_faces(self):
        graph = return_graph()
        request, ports, _ = _request_graph(graph)
        sides = {port["id"]: port["layoutOptions"]["elk.port.side"]
                 for node in request["children"] for port in node["ports"]}
        self.assertEqual(sides[ports["out:" + txid("start") + ":0"][1]], "WEST")
        self.assertEqual(sides[ports["out:" + txid("return") + ":0"][1]], "NORTH")
        self.assertEqual(sides[ports["in:" + txid("return") + ":0"][0]], "EAST")
        graph["graph_options"]["layout_style"] = "standard"
        request, ports, _ = _request_graph(graph)
        sides = {port["id"]: port["layoutOptions"]["elk.port.side"]
                 for node in request["children"] for port in node["ports"]}
        self.assertEqual(sides[ports["out:" + txid("return") + ":0"][1]], "EAST")

    def test_standard_style_retains_original_global_hub_behavior(self):
        graph = mixed_terminal_graph()
        graph["graph_options"]["layout_style"] = "standard"
        plan = hub_plan(graph)
        self.assertNotIn("entries", plan)
        for node in graph["nodes"]:
            if node["kind"] == "transaction":
                self.assertLess(plan["columns"][hub_id(HUB_A)], plan["columns"][node["id"]])
        before = copy.deepcopy(graph)
        graph["graph_options"].pop("layout_style")
        self.assertEqual(hub_plan(graph), plan)
        graph["graph_options"]["layout_style"] = "standard"
        self.assertEqual(graph, before)


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class TraceHubEngineTests(unittest.TestCase):
    def assert_evidence(self, graph, result):
        self.assertEqual({n["id"]: {k: v for k, v in n.items() if k not in ("x", "y")} for n in result["nodes"]},
                         {n["id"]: {k: v for k, v in n.items() if k not in ("x", "y")} for n in graph["nodes"]})
        geometry = {"attachment", "route", "connector_shape", "routing_exception", "label_layout"}
        self.assertEqual({e["id"]: {k: v for k, v in e.items() if k not in geometry} for e in result["edges"]},
                         {e["id"]: {k: v for k, v in e.items() if k not in geometry} for e in graph["edges"]})
        self.assertNotIn("_hub_layout_plan", result)
        self.assertNotIn("_hub_layout_view", result)

    def test_real_elk_places_seed_before_hub_and_stacks_mixed_endpoints(self):
        graph = mixed_terminal_graph()
        original = copy.deepcopy(graph)
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        nodes = {node["id"]: node for node in result["nodes"]}
        txs = [node_id(name) for name in ("pegout", "opreturn", "unspent")]
        ends = ["event:" + txid("pegout") + ":0", "event:" + txid("opreturn") + ":0", "liquid:address:" + DORMANT]
        self.assertLess(nodes[node_id("start")]["x"], nodes[hub_id(HUB_A)]["x"])
        self.assertEqual(len({round(nodes[key]["x"], 5) for key in txs}), 1)
        self.assertEqual(len({round(nodes[key]["x"], 5) for key in ends}), 1)
        for key, end in zip(txs, ends):
            self.assertLess(nodes[hub_id(HUB_A)]["x"], nodes[key]["x"])
            self.assertLess(nodes[key]["x"], nodes[end]["x"])
            self.assertLess(abs(nodes[key]["y"] - nodes[end]["y"]), 81)
        self.assertEqual(layout_metrics(result)["node_overlaps"], 0)
        self.assertEqual(graph, original)
        self.assert_evidence(graph, result)

    def test_real_elk_distinguishes_initial_deposit_from_actual_return_route(self):
        graph = return_graph()
        original = copy.deepcopy(graph)
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        nodes = {node["id"]: node for node in result["nodes"]}
        edges = {edge["id"]: edge for edge in result["edges"]}
        initial = edges["out:" + txid("start") + ":0"]
        returning = edges["out:" + txid("return") + ":0"]
        self.assertNotEqual(initial["routing_exception"], "return")
        self.assertEqual(returning["routing_exception"], "return")
        self.assertLess(float(initial["attachment"]["endItem"]["position"]["x"].rstrip("%")), 50)
        position = returning["attachment"]["endItem"]["position"]
        self.assertGreater(abs(float(position["y"].rstrip("%")) - 50),
                           abs(float(position["x"].rstrip("%")) - 50))
        for edge in result["edges"]:
            self.assertEqual(edge["route"][0], attachment_point(nodes[edge["source"]], edge["attachment"]["startItem"]))
            self.assertEqual(edge["route"][-1], attachment_point(nodes[edge["target"]], edge["attachment"]["endItem"]))
        self.assertEqual(graph, original)
        self.assert_evidence(graph, result)


if __name__ == "__main__":
    unittest.main()
