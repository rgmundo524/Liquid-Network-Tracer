"""Trace layout uses exact continuations and never changes graph evidence."""

import copy
import json
import unittest

from liquid_tracer.trace_layout import (SPINE_STRAIGHTNESS, trace_metrics, trace_order,
                                        trace_priorities, trace_structure)


def fixture():
    graph = {"nodes": [], "edges": [], "fee_items": {}, "graph_options": {"layout_style": "trace"}}
    def node(key, kind, column, **extra):
        graph["nodes"].append({"id": key, "kind": kind, "column": column,
                               "x": column * 300, "y": 0, "width": 160, "height": 160,
                               "details": {}, **extra})
    def edge(key, source, target, outpoint, **extra):
        graph["edges"].append({"id": key, "source": source, "target": target,
                               "outpoint": outpoint, **extra})
    node("root", "transaction", 0, role="starting_transaction")
    for name, length in (("a", 3), ("b", 2)):
        parent = "root"
        for index in range(length):
            address, tx = f"{name}-addr-{index}", f"{name}-tx-{index}"
            node(address, "address", index * 2 + 1)
            node(tx, "transaction", index * 2 + 2)
            outpoint = f"{parent}:{name}"
            edge(f"{address}-out", parent, address, outpoint)
            edge(f"{address}-in", address, tx, outpoint, role="traced_input")
            parent = tx
        node(f"{name}-end", "event", length * 2 + 1)
        edge(f"{name}-end-out", parent, f"{name}-end", f"{parent}:0")
    return graph, node, edge


class TraceLayoutTests(unittest.TestCase):
    def test_disabled_and_empty_graphs_have_no_layout_preferences(self):
        graph, _, _ = fixture()
        del graph["graph_options"]["layout_style"]
        self.assertIsNone(trace_order(graph))
        self.assertEqual(trace_priorities(graph), {})
        self.assertFalse(trace_metrics(graph)["enabled"])
        empty = {"nodes": [], "edges": [], "graph_options": {"layout_style": "trace"}}
        self.assertEqual(trace_order(empty), [])
        self.assertEqual(trace_metrics(empty)["spine_alignment"], 0)

    def test_longest_exact_seed_path_keeps_other_fork_in_one_branch(self):
        graph, _, _ = fixture()
        original = copy.deepcopy(graph)
        structure = trace_structure(graph)
        self.assertEqual(structure["spine"], ["root", "a-addr-0", "a-tx-0", "a-addr-1",
                                              "a-tx-1", "a-addr-2", "a-tx-2", "a-end"])
        branch = next(group for group in structure["branches"] if "b-tx-0" in group)
        self.assertEqual(branch, {"b-addr-0", "b-tx-0", "b-addr-1", "b-tx-1", "b-end"})
        order = trace_order(graph)
        self.assertEqual(set(order), {node["id"] for node in graph["nodes"]})
        self.assertEqual(len(order), len(set(order)))
        priorities = trace_priorities(graph)
        self.assertEqual(priorities["a-addr-1-in"], SPINE_STRAIGHTNESS)
        self.assertNotIn("b-addr-1-in", priorities)
        trace_metrics(graph)
        self.assertEqual(graph, original)

    def test_reused_address_does_not_create_a_path_for_unrelated_outpoint(self):
        graph, node, edge = fixture()
        node("unrelated", "transaction", 20)
        edge("unrelated-in", "a-addr-2", "unrelated", "another-producer:0", role="traced_input")
        structure = trace_structure(graph)
        self.assertNotIn("unrelated", structure["core"])
        self.assertNotIn("unrelated-in", structure["edges"])

    def test_ambiguous_spenders_and_pegin_cannot_extend_spine(self):
        for mode in ("double-spender", "pegin", "coinbase", "backward"):
            with self.subTest(mode=mode):
                graph, node, edge = fixture()
                target = next(item for item in graph["edges"] if item["id"] == "a-addr-1-in")
                if mode == "double-spender":
                    node("duplicate", "transaction", 6)
                    edge("duplicate-input", "a-addr-1", "duplicate", target["outpoint"])
                elif mode in ("pegin", "coinbase"):
                    target["details"] = {"vin": {"is_" + mode: True}}
                else:
                    next(item for item in graph["nodes"] if item["id"] == "a-tx-1")["column"] = 0
                structure = trace_structure(graph)
                self.assertNotIn("a-tx-1", structure["core"])
                self.assertNotIn("a-addr-1-in", structure["edges"])

    def test_named_group_and_explicit_change_override_longer_fork(self):
        graph, _, _ = fixture()
        graph["graph_options"]["center_name"] = " Example "
        target = next(item for item in graph["nodes"] if item["id"] == "b-addr-0")
        target["details"] = {"address_attributions": [{"entity": "example"}]}
        self.assertIn("b-tx-1", trace_structure(graph)["core"])
        graph["graph_options"]["center_name"] = ""
        next(item for item in graph["edges"] if item["id"] == "b-addr-0-out")["change_output"] = {"vout": 1}
        self.assertIn("b-tx-1", trace_structure(graph)["core"])

    def test_longer_unrelated_context_chain_never_displaces_seed_path(self):
        graph, node, edge = fixture()
        node("context-root", "transaction", -2)
        parent = "context-root"
        for index in range(30):
            address, tx = f"context-{index}", f"context-tx-{index}"
            node(address, "address", index * 2 - 1)
            node(tx, "transaction", index * 2)
            edge(address + "-out", parent, address, parent + ":0")
            edge(address + "-in", address, tx, parent + ":0")
            parent = tx
        self.assertIn("root", trace_structure(graph)["core"])
        self.assertNotIn("context-root", trace_structure(graph)["core"])
        # Serialized focused graphs also identify the root via seed output.
        next(item for item in graph["nodes"] if item["id"] == "root").pop("role")
        next(item for item in graph["edges"] if item["id"] == "a-addr-0-out")["role"] = "seed_output"
        self.assertIn("root", trace_structure(graph)["core"])

    def test_shared_context_does_not_glue_branches_or_duplicate_hub(self):
        graph, node, edge = fixture()
        node("c-addr", "address", 1)
        node("c-tx", "transaction", 2)
        edge("c-out", "root", "c-addr", "root:c")
        edge("c-in", "c-addr", "c-tx", "root:c")
        node("shared", "address", 1)
        edge("shared-b", "shared", "b-tx-0", "external:0")
        edge("shared-c", "shared", "c-tx", "external:1")
        structure = trace_structure(graph)
        self.assertIn("shared", structure["peripheral"])
        self.assertFalse(any({"b-tx-0", "c-tx"} <= group for group in structure["branches"]))
        self.assertEqual(trace_order(graph).count("shared"), 1)
        self.assertEqual(len(graph["edges"]), 16)

    def test_unshared_backbone_beats_longer_repeated_address_sequence(self):
        graph, _, _ = fixture()
        old_ids = {"a-addr-0", "a-addr-1", "a-addr-2"}
        template = copy.deepcopy(next(node for node in graph["nodes"] if node["id"] == "a-addr-0"))
        template["id"] = "shared-payout"
        graph["nodes"] = [node for node in graph["nodes"] if node["id"] not in old_ids] + [template]
        for edge in graph["edges"]:
            for field in ("source", "target"):
                if edge[field] in old_ids:
                    edge[field] = "shared-payout"
        original = copy.deepcopy(graph)
        structure = trace_structure(graph)
        self.assertIn("b-tx-1", structure["core"])
        self.assertNotIn("a-tx-2", structure["core"])
        self.assertEqual(structure["shared_hubs"], {"shared-payout"})
        self.assertIn("shared-payout", structure["peripheral"])
        self.assertNotIn("shared-payout", structure["core"])
        self.assertEqual(trace_order(graph).count("shared-payout"), 1)
        self.assertTrue(all(not key.startswith("a-addr-") for key in trace_priorities(graph)))
        self.assertEqual(graph, original)

        # An explicit designation still takes precedence over the automatic
        # backbone choice, without pulling the shared circle into its center.
        next(edge for edge in graph["edges"] if edge["id"] == "a-addr-0-out")["change_output"] = {"vout": 0}
        structure = trace_structure(graph)
        self.assertIn("a-tx-2", structure["core"])
        self.assertNotIn("shared-payout", structure["core"])
        self.assertTrue(all(not key.startswith("a-addr-") for key in trace_priorities(graph)))

    def test_explicit_hub_and_fee_do_not_become_spine_members(self):
        graph, node, edge = fixture()
        next(item for item in graph["nodes"] if item["id"] == "a-addr-1")["layout_hub"] = True
        node("fee", "event", 1)
        edge("fee-out", "root", "fee", "root:fee")
        graph["fee_items"]["fee"] = {"endpoint": "shapes"}
        structure = trace_structure(graph)
        self.assertNotIn("a-addr-1", structure["core"])
        self.assertNotIn("fee", structure["core"])
        self.assertNotIn(("root", "fee"), structure["terminal_pairs"])
        self.assertEqual(set(trace_order(graph)), {node["id"] for node in graph["nodes"]})

    def test_terminal_proximity_and_spine_drift_are_mean_normalized_metrics(self):
        graph, _, _ = fixture()
        structure = trace_structure(graph)
        before = trace_metrics(graph, structure)
        next(item for item in graph["nodes"] if item["id"] == "b-end")["y"] = 2000
        self.assertGreater(trace_metrics(graph, structure)["terminal_distance"], before["terminal_distance"])
        next(item for item in graph["nodes"] if item["id"] == "a-tx-1")["y"] = 800
        self.assertGreater(trace_metrics(graph, structure)["spine_alignment"], before["spine_alignment"])
        json.dumps(trace_metrics(graph, structure))
        self.assertEqual(before["terminal_pairs"], 2)

    def test_equal_disconnected_graphs_do_not_double_mean_terminal_cost(self):
        graph, _, _ = fixture()
        before = trace_metrics(graph)
        second = copy.deepcopy(graph)
        for node in second["nodes"]:
            node["id"] = "copy-" + node["id"]
            node["x"] += 10000
            node["y"] += 10000
        for edge in second["edges"]:
            for field in ("id", "source", "target", "outpoint"):
                edge[field] = "copy-" + edge[field]
        graph["nodes"].extend(second["nodes"])
        graph["edges"].extend(second["edges"])
        after = trace_metrics(graph)
        self.assertEqual(after["terminal_distance"], before["terminal_distance"])
        self.assertEqual(after["terminal_pairs"], 2 * before["terminal_pairs"])

    def test_core_or_shared_node_between_siblings_counts_as_interleaving(self):
        graph, node, edge = fixture()
        node("b-context-one", "address", 1)
        node("b-context-two", "address", 1)
        edge("b-context-one-in", "b-context-one", "b-tx-0", "outside:1")
        edge("b-context-two-in", "b-context-two", "b-tx-0", "outside:2")
        for item in graph["nodes"]:
            if item["id"] == "b-context-one":
                item["y"] = -500
            elif item["id"] == "b-context-two":
                item["y"] = 500
        self.assertGreater(trace_metrics(graph)["branch_interleaving"], 0)
        next(item for item in graph["nodes"] if item["id"] == "a-addr-0")["y"] = -1000
        self.assertEqual(trace_metrics(graph)["branch_interleaving"], 0)

    def test_serialized_node_and_edge_order_never_changes_preferences(self):
        graph, _, _ = fixture()
        expected = trace_structure(graph), trace_order(graph), trace_priorities(graph), trace_metrics(graph)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual((trace_structure(graph), trace_order(graph), trace_priorities(graph), trace_metrics(graph)), expected)

    def test_explicit_hub_metrics_use_rebased_columns(self):
        from liquid_tracer.hub_layout import hub_layout_view, hub_plan
        from tests.test_hub_layout import graph_from, node_id, transaction

        graph = graph_from([
            transaction("deposit", [("outside", 0, "outside")], ["hub"]),
            transaction("branch-a", [("deposit", 0, "hub"), ("external-a", 0, "context-a1"),
                                      ("external-b", 0, "context-a2")], ["hub"]),
            transaction("branch-b", [("branch-a", 0, "hub"), ("external-c", 0, "context-b")], ["end"]),
        ], {"hub"})
        graph["graph_options"]["layout_style"] = "trace"
        for node in graph["nodes"]:
            if node["id"] == node_id("deposit"):
                node["role"] = "starting_transaction"
            node["y"] = {"context-a1": 0, "context-a2": 200, "context-b": 100}.get(
                node.get("details", {}).get("address"), 1000)
        original = copy.deepcopy(graph)
        self.assertEqual(len(hub_plan(graph)["cut_inputs"]), 2)
        # These inputs occupy different original columns, but the hub reset
        # puts them together. Branch B now interrupts branch A's two inputs.
        rebased = trace_metrics(hub_layout_view(graph))
        self.assertGreater(rebased["branch_interleaving"], 0)
        self.assertEqual(trace_metrics(graph), rebased)
        self.assertEqual(trace_metrics(graph, trace_structure(graph)), rebased)
        self.assertEqual(graph, original)

    def test_long_chain_uses_iterative_paths(self):
        graph = {"nodes": [], "edges": [], "graph_options": {"layout_style": "trace"}}
        for index in range(2401):
            graph["nodes"].append({"id": str(index), "kind": "transaction" if index % 2 == 0 else "address",
                                   "column": index, "x": index * 300, "y": 0, "width": 160, "height": 160})
            if index:
                graph["edges"].append({"id": str(index), "source": str(index - 1), "target": str(index),
                                       "outpoint": f"{index - (1 if index % 2 else 2)}:0"})
        self.assertEqual(len(trace_structure(graph)["spine"]), 2401)
        self.assertEqual(len(trace_order(graph)), 2401)
        self.assertEqual(trace_metrics(graph)["spine_alignment"], 0)


if __name__ == "__main__":
    unittest.main()
