"""Starter plots merge display addresses without changing UTXO evidence."""
from copy import deepcopy
import unittest

from liquid_tracer.connections import connection_graph, connecting_outpoints
from liquid_tracer.export import short_address
from liquid_tracer.miro import make_plan, validate_plan
from tests.test_attribution_convergence import annotation, graph_state, tx
from tests.test_pegout_paths import set_address


class SharedAddressConnectionTests(unittest.TestCase):
    def test_parallel_shared_outputs_keep_every_connector_in_each_scope(self):
        state = graph_state((("a:0", "b"), ("a:1", "b")),
                            seeds=("a:0", "a:1", "b:0"))
        address = "SYNTHETIC-shared-receiver"
        for index in (0, 1):
            set_address(state, f"{tx('a')}:{index}", address)
        before = deepcopy(state)
        for scope in (None, "all_saved", "hop_limited"):
            with self.subTest(scope=scope):
                graph = connection_graph(state, 1, connection_scope=scope,
                                         transaction_io="complete")
                shared, = [node for node in graph["nodes"]
                           if node["details"].get("address") == address]
                self.assertEqual(graph["address_mode"], "merged")
                self.assertEqual({item["outpoint"] for item in shared["details"]["occurrences"]},
                                 {tx("a") + ":0", tx("a") + ":1"})
                attached = [edge for edge in graph["edges"]
                            if shared["id"] in (edge["source"], edge["target"])]
                self.assertEqual({edge["id"] for edge in attached},
                                 {f"{side}:{tx(name)}:{index}"
                                  for side, name in (("out", "a"), ("in", "b"))
                                  for index in (0, 1)})
                self.assertEqual({edge["outpoint"] for edge in attached},
                                 {tx("a") + ":0", tx("a") + ":1"})
                plan = make_plan(graph)
                validate_plan(plan)
                self.assertEqual(len(plan["connectors"]), 5)
                self.assertEqual(sum(shape["body"]["data"]["shape"] == "circle"
                                     for shape in plan["shapes"]), 2)
        self.assertEqual(state, before)

    def test_address_reuse_across_hops_keeps_report_and_requested_occurrences(self):
        state = graph_state((("a:0", "c"), ("c:0", "b")))
        address = "SYNTHETIC-reused-across-hops"
        for name in "acb":
            set_address(state, tx(name) + ":0", address)
        before = deepcopy(state)
        report = connecting_outpoints(state, connection_scope="all_saved")
        for transaction_io in (None, "complete"):
            with self.subTest(transaction_io=transaction_io):
                graph = connection_graph(state, connection_scope="all_saved",
                                         transaction_io=transaction_io)
                node, = [node for node in graph["nodes"] if node["kind"] == "address"]
                names = "acb" if transaction_io else "ac"
                self.assertEqual({item["outpoint"] for item in node["details"]["occurrences"]},
                                 {tx(name) + ":0" for name in names})
                self.assertEqual({edge["id"] for edge in graph["edges"]},
                                 {"out:" + tx("a") + ":0", "in:" + tx("c") + ":0",
                                  "out:" + tx("c") + ":0", "in:" + tx("b") + ":0"}
                                 | ({"out:" + tx("b") + ":0"} if transaction_io else set()))
                for key, value in report.items():
                    self.assertEqual(graph["connections"][key], value)
                validate_plan(make_plan(graph))
        self.assertEqual(state, before)

    def test_excluded_seed_and_coinput_cannot_change_shared_path_node(self):
        state = graph_state((("a:0", "c"), ("c:0", "b")), raw_links=(("f:0", "c"),))
        address = "SYNTHETIC-shared-with-excluded-context"
        for name in "cbf":
            set_address(state, tx(name) + ":0", address)
        state["labels"] = [{**annotation(stop=False, name="Excluded " + name),
                            "kind": "outpoint", "value": tx(name) + ":0"}
                           for name in "bf"]
        graph = connection_graph(state, 2)
        shared, = [node for node in graph["nodes"] if node["details"].get("address") == address]
        self.assertEqual(shared["role"], "candidate")
        self.assertEqual([item["outpoint"] for item in shared["details"]["occurrences"]],
                         [tx("c") + ":0"])
        self.assertNotIn("address_attributions", shared["details"])
        self.assertNotIn("Excluded", shared["label"])
        self.assertEqual(len(graph["edges"]), 4)
        complete = connection_graph(state, 2, transaction_io="complete")
        context, = [node for node in complete["nodes"]
                    if node["details"].get("address") == address]
        self.assertEqual(context["role"], "seed")
        self.assertEqual({item["outpoint"] for item in context["details"]["occurrences"]},
                         {tx(name) + ":0" for name in "cbf"})
        self.assertEqual({item["entity"] for item in context["details"]["address_attributions"]},
                         {"Excluded b", "Excluded f"})

    def test_full_address_identity_and_unknown_outpoints_stay_distinct(self):
        state = graph_state(tuple((f"a:{index}", "b") for index in range(4)),
                            seeds=tuple(f"a:{index}" for index in range(4)) + ("b:0",))
        addresses = ["SYNTHETIC-" + middle + "-abcdefg" for middle in ("11111", "22222")]
        self.assertEqual(short_address(addresses[0]), short_address(addresses[1]))
        for index, address in enumerate(addresses):
            set_address(state, f"{tx('a')}:{index}", address)
        for index in (2, 3):
            state["transactions"][tx("a")]["data"]["vout"][index].pop("scriptpubkey_address")
            state["transactions"][tx("b")]["data"]["vin"][index]["prevout"].pop(
                "scriptpubkey_address", None)
        graph = connection_graph(state, connection_scope="all_saved")
        self.assertEqual({node["id"] for node in graph["nodes"] if node["kind"] == "address"},
                         {"liquid:address:" + address for address in addresses}
                         | {f"liquid:outpoint:{tx('a')}:{index}" for index in (2, 3)})
        self.assertEqual(len(graph["edges"]), 8)
        validate_plan(make_plan(graph))

    def test_complete_context_seed_does_not_add_lineage_to_shared_path_address(self):
        state = graph_state((("a:0", "c"), ("c:0", "b")))
        address = "SYNTHETIC-shared-with-ending-seed"
        for name in "cb":
            set_address(state, tx(name) + ":0", address)
        graph = connection_graph(state, connection_scope="all_saved", transaction_io="complete")
        shared, = [node for node in graph["nodes"] if node["details"].get("address") == address]
        branch = graph["branch_structure"]
        self.assertEqual(branch["node_memberships"][shared["id"]], ["tx:" + tx("a")])
        self.assertNotIn("out:" + tx("b") + ":0", branch["edge_memberships"])
        self.assertEqual(branch["node_memberships"]["tx:" + tx("b")],
                         ["tx:" + tx("a"), "tx:" + tx("b")])
        self.assertEqual({root["key"] for root in branch["roots"]},
                         {"tx:" + tx("a"), "tx:" + tx("b")})


if __name__ == "__main__":
    unittest.main()
