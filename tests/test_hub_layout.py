import copy
import unittest

from liquid_tracer.export import build_graph
from liquid_tracer.compaction import compact_graph
from liquid_tracer.elk_layout import attachment_point, layout_metrics, optimize_graph
from liquid_tracer.hub_layout import hub_layout_view, hub_plan
from tests.fixtures import output
from tests.test_elk_layout import HAS_ELK
from tests.test_layout import chain, state_from, txid


def transaction(name, inputs, addresses):
    key = txid(name)
    return {"txid": key,
            "vin": [{"txid": txid(parent), "vout": index, "prevout": output(address)}
                    for parent, index, address in inputs],
            "vout": [output(address) for address in addresses], "status": {}}


def graph_from(values, hubs):
    graph = build_graph(state_from({value["txid"]: value for value in values}))
    for node in graph["nodes"]:
        if node.get("details", {}).get("address") in hubs:
            node["layout_hub"] = True
    return graph


def node_id(name):
    return "tx:" + txid(name)


def busy_hub_graph():
    transactions = chain(7)
    for index in range(7):
        value = transactions[txid(index)]
        value["vin"].append({"txid": txid("context-" + str(index)), "vout": 0,
                             "prevout": output("SYNTHETIC-context-" + str(index))})
        payment = "SYNTHETIC-payment-" + str(index)
        value["vout"].append(output(payment))
        if index % 2 == 0:
            descendant = transaction("descendant-" + str(index), [(index, 1, payment)],
                                     ["SYNTHETIC-terminal-" + str(index)])
            transactions[descendant["txid"]] = descendant
    graph = build_graph(state_from(transactions))
    hub = next(node for node in graph["nodes"]
               if node.get("details", {}).get("address") == "SYNTHETIC-reused-address")
    hub["layout_hub"] = True
    return graph


class HubPlanTests(unittest.TestCase):
    def test_unselected_and_non_liquid_addresses_do_not_reset_dependencies(self):
        graph = build_graph(state_from(chain(5)))
        self.assertEqual(hub_plan(graph), {"columns": {}, "cut_inputs": set(), "hubs": [], "roots": {}})
        self.assertIs(hub_layout_view(graph), graph)
        hub = next(node for node in graph["nodes"] if node["kind"] == "address")
        hub.update(layout_hub=True)
        hub["details"]["network"] = "bitcoin"
        self.assertEqual(hub_plan(graph)["hubs"], [])

    def test_hub_chain_starts_one_transaction_column_without_evidence_changes(self):
        graph = build_graph(state_from(chain(5)))
        hub = next(node for node in graph["nodes"] if node["kind"] == "address")
        hub["layout_hub"] = True
        before = copy.deepcopy(graph)
        plan = hub_plan(graph)
        transactions = {node["id"] for node in graph["nodes"] if node["kind"] == "transaction"}
        self.assertEqual({plan["columns"][key] for key in transactions}, {1})
        self.assertEqual(plan["cut_inputs"], {(node_id(index), 0) for index in range(1, 5)})
        self.assertEqual(set(plan["roots"][hub["id"]]), transactions)
        self.assertLess(plan["columns"][hub["id"]], 1)
        self.assertEqual(graph, before)
        view = hub_layout_view(graph)
        self.assertEqual(graph, before)
        self.assertIs(hub_layout_view(view), view)
        self.assertEqual(hub_plan(view), plan)
        self.assertEqual([node["details"] for node in view["nodes"]],
                         [node["details"] for node in graph["nodes"]])
        self.assertEqual(view["edges"], graph["edges"])
        self.assertNotIn("_hub_layout_view", graph)

    def test_other_input_from_same_parent_retains_dependency(self):
        graph = graph_from([
            transaction("parent", [("external", 0, "outside")], ["hub", "ordinary"]),
            transaction("child", [("parent", 0, "hub"), ("parent", 1, "ordinary")], ["end"]),
        ], {"hub"})
        plan = hub_plan(graph)
        self.assertEqual(plan["cut_inputs"], {(node_id("child"), 0)})
        self.assertEqual(plan["columns"][node_id("parent")], 1)
        self.assertEqual(plan["columns"][node_id("child")], 3)

    def test_serialized_view_marker_cannot_supply_unverified_dependency_cuts(self):
        graph = graph_from([
            transaction("parent", [("external", 0, "outside")], ["hub", "ordinary"]),
            transaction("child", [("parent", 0, "hub"), ("parent", 1, "ordinary")], ["end"]),
        ], {"hub"})
        expected = hub_plan(graph)
        graph.update(_hub_layout_view=True,
                     _hub_layout_plan={"columns": {node_id("child"): -100},
                                       "cut_inputs": {(node_id("child"), 1)},
                                       "hubs": [], "roots": {}})
        self.assertEqual(hub_plan(graph), expected)
        view = hub_layout_view(graph)
        self.assertIsNot(view, graph)
        self.assertEqual(hub_plan(view), expected)
        self.assertIs(hub_layout_view(view), view)
        self.assertEqual(expected["cut_inputs"], {(node_id("child"), 0)})

    def test_non_hub_descendants_and_rejoin_keep_forward_dependencies(self):
        graph = graph_from([
            transaction("deposit", [("external", 0, "outside")], ["hub", "ordinary"]),
            transaction("restart", [("deposit", 0, "hub")], ["first"]),
            transaction("descendant", [("restart", 0, "first")], ["second"]),
            transaction("join", [("descendant", 0, "second"), ("deposit", 1, "ordinary")], ["end"]),
        ], {"hub"})
        plan = hub_plan(graph)
        self.assertEqual(plan["cut_inputs"], {(node_id("restart"), 0)})
        self.assertEqual([plan["columns"][node_id(name)] for name in
                          ("deposit", "restart", "descendant", "join")], [1, 1, 3, 5])
        first = next(node for node in graph["nodes"] if node.get("details", {}).get("address") == "first")
        self.assertEqual(plan["columns"][first["id"]], 2)

    def test_multiple_hubs_reset_independently_and_preserve_common_coinput(self):
        graph = graph_from([
            transaction("start", [("external", 0, "outside")], ["hub-a", "ordinary"]),
            transaction("next", [("start", 0, "hub-a")], ["hub-b"]),
            transaction("joined", [("next", 0, "hub-b"), ("start", 1, "ordinary")], ["end"]),
        ], {"hub-a", "hub-b"})
        plan = hub_plan(graph)
        self.assertEqual(len(plan["hubs"]), 2)
        self.assertEqual(plan["cut_inputs"], {(node_id("next"), 0), (node_id("joined"), 0)})
        self.assertEqual([plan["columns"][node_id(name)] for name in ("start", "next", "joined")], [1, 1, 3])
        self.assertEqual(len(set(plan["columns"][key] for key in plan["hubs"])), 1)

    def test_exact_input_and_output_match_required_for_cut(self):
        original = graph_from([
            transaction("parent", [("external", 0, "outside")], ["hub", "ordinary"]),
            transaction("child", [("parent", 0, "hub")], ["end"]),
        ], {"hub"})
        input_id, output_id = "in:" + txid("child") + ":0", "out:" + txid("parent") + ":0"
        ordinary = next(node["id"] for node in original["nodes"]
                        if node.get("details", {}).get("address") == "ordinary")
        for change in ("input_outpoint", "input_vin", "input_target", "output_target",
                       "missing_output", "ambiguous_output", "ambiguous_input"):
            with self.subTest(change=change):
                graph = copy.deepcopy(original)
                edges = {edge["id"]: edge for edge in graph["edges"]}
                if change == "input_outpoint":
                    edges[input_id]["outpoint"] = txid("parent") + ":1"
                elif change == "input_vin":
                    edges[input_id]["details"]["vin"] = {"txid": txid("parent"), "vout": 1}
                elif change == "input_target":
                    edges[input_id]["target"] = node_id("parent")
                elif change == "output_target":
                    edges[output_id]["target"] = ordinary
                elif change == "missing_output":
                    graph["edges"].remove(edges[output_id])
                elif change == "ambiguous_output":
                    graph["edges"].append({**edges[output_id], "id": "conflicting-output", "target": ordinary})
                else:
                    graph["edges"].append(copy.deepcopy(edges[input_id]))
                plan = hub_plan(graph)
                self.assertEqual(plan["cut_inputs"], set())
                for node in graph["nodes"]:
                    if node["id"] not in plan["hubs"]:
                        self.assertEqual(plan["columns"][node["id"]], node["column"])

    def test_pegin_coinbase_and_malformed_outpoint_are_never_hub_cuts(self):
        original = graph_from([
            transaction("parent", [("external", 0, "outside")], ["hub"]),
            transaction("child", [("parent", 0, "hub")], ["end"]),
        ], {"hub"})
        for field, value in (("is_pegin", True), ("is_coinbase", True), ("vout", True), ("vout", -1)):
            with self.subTest(field=field, value=value):
                graph = copy.deepcopy(original)
                child = next(node for node in graph["nodes"] if node["id"] == node_id("child"))
                child["details"]["transaction"]["vin"][0][field] = value
                self.assertEqual(hub_plan(graph)["cut_inputs"], set())

    def test_deterministic_order_and_no_cuts_preserve_original_columns(self):
        graph = build_graph(state_from(chain(5)))
        hub = next(node for node in graph["nodes"] if node["kind"] == "address")
        hub["layout_hub"] = True
        expected = hub_plan(graph)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(hub_plan(graph), expected)
        graph["edges"] = [edge for edge in graph["edges"] if edge["source"] != hub["id"]]
        plan = hub_plan(graph)
        self.assertFalse(plan["cut_inputs"])
        self.assertEqual({key: value for key, value in plan["columns"].items() if key != hub["id"]},
                         {node["id"]: node["column"] for node in graph["nodes"] if node["id"] != hub["id"]})

    def test_deep_hub_chain_uses_iterative_ranking_and_filtered_graph_stays_left(self):
        graph = build_graph(state_from(chain(1100)))
        hub = next(node for node in graph["nodes"] if node["kind"] == "address")
        hub["layout_hub"] = True
        for node in graph["nodes"]:
            node["column"] += 100
        plan = hub_plan(graph)
        self.assertEqual(len(plan["cut_inputs"]), 1099)
        self.assertTrue(all(plan["columns"][hub["id"]] < plan["columns"][node["id"]]
                            for node in graph["nodes"] if node["kind"] == "transaction"))


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class HubEngineTests(unittest.TestCase):
    def test_busy_hub_with_context_and_unequal_branch_depth_keeps_vertical_spenders(self):
        graph = busy_hub_graph()
        before = copy.deepcopy(graph)
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(len({round(nodes[node_id(index)]["x"], 5) for index in range(7)}), 1)
        for index in range(0, 7, 2):
            payment = next(node for node in result["nodes"]
                           if node.get("details", {}).get("address") == "SYNTHETIC-payment-" + str(index))
            self.assertLess(nodes[node_id(index)]["x"], payment["x"])
            self.assertLess(payment["x"], nodes[node_id("descendant-" + str(index))]["x"])
        metrics = layout_metrics(result)
        self.assertEqual(metrics["node_overlaps"], 0)
        self.assertEqual(metrics["node_intersections"], 0)
        self.assertEqual(result["layout"]["branch_organization"]["neighborhoods"]["sibling_interleavings"], 0)
        self.assertEqual(graph, before)
        compacted = compact_graph(result)
        compacted_nodes = {node["id"]: node for node in compacted["nodes"]}
        self.assertEqual(len({round(compacted_nodes[node_id(index)]["x"], 5) for index in range(7)}), 1)

    def test_one_attempt_aligns_direct_spenders_and_retains_every_return(self):
        graph = build_graph(state_from(chain(5)))
        hub = next(node for node in graph["nodes"] if node["kind"] == "address")
        hub["layout_hub"] = True
        before = copy.deepcopy(graph)
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        nodes = {node["id"]: node for node in result["nodes"]}
        transactions = [node for node in result["nodes"] if node["kind"] == "transaction"]
        self.assertEqual(len({round(node["x"], 5) for node in transactions}), 1)
        self.assertTrue(all(nodes[hub["id"]]["x"] < node["x"] for node in transactions))
        self.assertEqual(layout_metrics(result)["node_overlaps"], 0)
        self.assertEqual(graph, before)
        self.assertNotIn("_hub_layout_view", result)
        self.assertNotIn("_hub_layout_plan", result)
        self.assertEqual({(edge["id"], edge["source"], edge["target"], edge["outpoint"])
                          for edge in result["edges"]},
                         {(edge["id"], edge["source"], edge["target"], edge["outpoint"])
                          for edge in graph["edges"]})
        for node in graph["nodes"]:
            self.assertEqual({key: value for key, value in nodes[node["id"]].items() if key not in ("x", "y")},
                             {key: value for key, value in node.items() if key not in ("x", "y")})
        for edge in result["edges"]:
            if edge["target"] == hub["id"]:
                self.assertEqual(edge["routing_exception"], "return")
            self.assertEqual(edge["route"][0], attachment_point(nodes[edge["source"]], edge["attachment"]["startItem"]))
            self.assertEqual(edge["route"][-1], attachment_point(nodes[edge["target"]], edge["attachment"]["endItem"]))
        compacted = compact_graph(result)
        self.assertEqual(len({round(node["x"], 5) for node in compacted["nodes"]
                              if node["kind"] == "transaction"}), 1)
        self.assertNotIn("_hub_layout_view", compacted)

    def test_non_hub_coinput_keeps_spender_to_right_of_parent(self):
        graph = graph_from([
            transaction("parent", [("external", 0, "outside")], ["hub", "ordinary"]),
            transaction("child", [("parent", 0, "hub"), ("parent", 1, "ordinary")], ["end"]),
        ], {"hub"})
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertLess(nodes[node_id("parent")]["x"], nodes[node_id("child")]["x"])
        self.assertEqual(layout_metrics(result)["node_overlaps"], 0)


if __name__ == "__main__":
    unittest.main()
