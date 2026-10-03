"""Manual peg-out layout hubs preserve exact path evidence and identities."""
from copy import deepcopy
import unittest

from liquid_tracer.hub_layout import hub_plan
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from tests.test_attribution_convergence import graph_state, tx
from tests.test_pegout_paths import add_pegout, set_address


class PegoutHubTests(unittest.TestCase):
    def test_hubs_are_applied_after_path_selection_for_every_context_mode(self):
        address = "H" * 34
        state = graph_state((("a:0", "b"), ("a:1", "c"), ("b:0", "d")), seeds=("a:0", "a:1"))
        for key in (tx("a") + ":0", tx("a") + ":1", tx("b") + ":0"):
            set_address(state, key, address)
        for key in ("b", "c", "d"):
            add_pegout(state, tx(key))
        before = deepcopy(state)
        for context in ({}, {"include_context": True}, {"transaction_io": "complete"}):
            for grouped in (False, True):
                with self.subTest(context=context, grouped=grouped):
                    query = validate_query(seeds=state["seeds"], **context)
                    ordinary = pegout_graph(state, query, group_context_inputs=grouped)
                    graph = pegout_graph(state, query, group_context_inputs=grouped, hub_addresses=[address])
                    key = "liquid:address:" + address
                    self.assertEqual([node["id"] for node in graph["nodes"] if node.get("layout_hub")], [key])
                    layout = hub_plan(graph)
                    self.assertEqual(layout["roots"][key], sorted("tx:" + tx(name) for name in ("b", "c", "d")))
                    self.assertEqual(len(layout["cut_inputs"]), 3)
                    self.assertEqual(len({node["id"] for node in graph["nodes"]}), len(graph["nodes"]))
                    normalized = deepcopy(graph)
                    normalized["graph_options"].pop("hub_addresses")
                    for node in normalized["nodes"]:
                        node.pop("layout_hub", None)
                    self.assertEqual(normalized, ordinary)
                    self.assertEqual(state, before)


if __name__ == "__main__":
    unittest.main()
