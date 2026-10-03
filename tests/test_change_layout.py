import copy
import unittest

from liquid_tracer.change_layout import apply_change_layout
from liquid_tracer.compaction import compact_graph
from liquid_tracer.elk_layout import optimize_graph, attachment_point, layout_metrics, segment_hits_node
from liquid_tracer.export import build_graph
from liquid_tracer.layout_reuse import _fingerprint
from liquid_tracer.layout_preview import layout_notice, _preview_html
from tests.fixtures import output
from tests.test_elk_layout import HAS_ELK
from tests.test_layout import state_from, txid


def change_state(length=2):
    transactions = {}
    for index in range(length):
        key = txid("change-" + str(index))
        previous = txid("change-" + str(index - 1))
        vin = [{"txid": previous, "vout": 1, "prevout": output("SYNTHETIC-change-" + str(index - 1))}]
        transactions[key] = {"txid": key, "vin": vin, "vout": [output("SYNTHETIC-payment-" + str(index)),
                             output("SYNTHETIC-change-" + str(index)), output("SYNTHETIC-other-" + str(index))], "status": {}}
    state = state_from(transactions)
    state["service_controls"] = {"change_outputs": {key: {"vout": 1, "notes": "Investigator assessment", "updated_at": "2026-09-19"}
                                                   for key in transactions}}
    return state


def assert_rows(test, graph, state):
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = {edge["id"]: edge for edge in graph["edges"]}
    for key, assignment in state["service_controls"]["change_outputs"].items():
        source = nodes["tx:" + key]
        edge = edges[f"out:{key}:{assignment['vout']}"]
        test.assertEqual(source["y"], nodes[edge["target"]]["y"])
        test.assertLess(source["x"], nodes[edge["target"]]["x"])
        test.assertEqual(edge["route"][0]["y"], edge["route"][-1]["y"])
        for other in graph["edges"]:
            if other["source"] == source["id"] and other["id"] != edge["id"]:
                test.assertGreater(nodes[other["target"]]["y"], source["y"])


class ChangeDesignationGraphTests(unittest.TestCase):
    def test_annotation_is_exact_output_only_and_never_mutates_evidence(self):
        state = change_state()
        original = copy.deepcopy(state)
        graph = build_graph(state)
        self.assertEqual(state, original)
        for edge in graph["edges"]:
            expected = edge["id"].startswith("out:") and edge["id"].endswith(":1")
            self.assertEqual("change_output" in edge, expected)
            self.assertEqual("Change" in edge["label"], expected)
        self.assertFalse(any("change_output" in node for node in graph["nodes"]))

    def test_unknown_invalid_and_fee_designations_are_explicitly_skipped(self):
        state = change_state(1)
        key = next(iter(state["transactions"]))
        state["service_controls"]["change_outputs"] = {txid("missing"): {"vout": 0}, key: {"vout": 50}}
        graph = build_graph(state)
        self.assertEqual(len(graph["change_outputs"]["skipped"]), 2)
        self.assertEqual(graph["change_outputs"]["designations"], [])
        state["transactions"][key]["data"]["vout"].append({"scriptpubkey": "", "value": 1})
        state["service_controls"]["change_outputs"] = {key: {"vout": 3}}
        graph = build_graph(state)
        self.assertIn("not a spendable", graph["change_outputs"]["skipped"][0]["reason"])

    def test_untagged_graph_postpass_is_exact_noop(self):
        state = change_state(1)
        state.pop("service_controls")
        graph = build_graph(state)
        original = copy.deepcopy(graph)
        self.assertIs(apply_change_layout(graph), graph)
        self.assertEqual(graph, original)

    def test_unknown_only_does_not_move_or_reroute_graph(self):
        state = change_state(1)
        state["service_controls"]["change_outputs"] = {txid("unknown"): {"vout": 0}}
        graph = build_graph(state)
        original = copy.deepcopy(graph)
        apply_change_layout(graph)
        self.assertEqual(graph["nodes"], original["nodes"])
        self.assertEqual(graph["edges"], original["edges"])
        self.assertEqual(graph["layout"]["change_outputs"]["applied"], [])

    def test_large_chain_constraints_do_not_use_recursive_traversal(self):
        count = 1800
        nodes, edges, entries = [], [], []
        for index in range(count):
            source, target = f"tx:{index}", f"address:{index}"
            for key, kind, x in ((source, "transaction", index * 600), (target, "address", index * 600 + 300)):
                nodes.append({"id": key, "kind": kind, "x": x, "y": 300 + index % 3 * 200,
                              "width": 100, "height": 100, "column": index * 2 + (kind == "address")})
            key = f"{index}:0"
            edge = {"id": "out:" + key, "source": source, "target": target, "outpoint": key}
            edges.append(edge)
            entries.append({"txid": str(index), "vout": 0, "outpoint": key, "edge_id": edge["id"]})
            if index:
                edges.append({"id": f"in:{index}", "source": f"address:{index - 1}", "target": source,
                              "outpoint": f"{index - 1}:0"})
        graph = {"nodes": nodes, "edges": edges, "layout": {}, "change_outputs": {"designations": entries, "skipped": []}}
        apply_change_layout(graph)
        self.assertEqual(len(graph["layout"]["change_outputs"]["applied"]), count)
        self.assertEqual(len({node["y"] for node in nodes}), 1)

    def test_change_spine_clears_intermediate_inputs_and_disconnected_objects_after_compaction(self):
        for input_y in (250, 300):
            with self.subTest(input_y=input_y):
                nodes = [{"id": key, "kind": kind, "x": x, "y": y, "column": column,
                          "width": 100, "height": 100, "label": "SYNTHETIC"}
                         for key, kind, x, y, column in (
                             ("a", "transaction", 0, 300, 1), ("b", "address", 1000, 600, 2),
                             ("c", "address", 500, input_y, 2), ("d", "transaction", 1500, 700, 3),
                             ("e", "address", 750, 300, 2))]
                graph = {"nodes": nodes, "edges": [
                    {"id": "out:t:0", "source": "a", "target": "b", "outpoint": "t:0"},
                    {"id": "in:d:0", "source": "b", "target": "d", "outpoint": "t:0"},
                    {"id": "in:d:1", "source": "c", "target": "d", "outpoint": "u:0"}],
                    "layout": {"algorithm": "elk_layered_v1"}, "graph_options": {"connector_style": "elbowed"},
                    "change_outputs": {"designations": [{"txid": "t", "vout": 0, "outpoint": "t:0", "edge_id": "out:t:0"}]}}
                apply_change_layout(graph)
                for result in (graph, compact_graph(graph)):
                    for edge in result["edges"][:2]:
                        self.assertEqual(edge["route"][0]["y"], edge["route"][-1]["y"])
                        for node in result["nodes"]:
                            if node["id"] not in (edge["source"], edge["target"]):
                                self.assertFalse(segment_hits_node(edge["route"][0], edge["route"][-1], node),
                                                 (edge["id"], node["id"]))


@unittest.skipUnless(HAS_ELK, "Local ELK dependencies unavailable")
class ChangeElkTests(unittest.TestCase):
    def test_change_chain_stays_horizontal_and_other_outputs_go_below(self):
        state = change_state(3)
        graph = build_graph(state)
        original = copy.deepcopy(graph)
        result = optimize_graph(graph, "elbowed", layout_attempts=3)
        self.assertEqual(graph, original)
        self.assertEqual(len(result["layout"]["change_outputs"]["applied"]), 3)
        self.assertEqual(result["layout"]["change_outputs"]["skipped"], [])
        assert_rows(self, result, state)
        self.assertEqual(layout_metrics(result)["node_overlaps"], 0)
        tx_nodes = [node for node in result["nodes"] if node["kind"] == "transaction"]
        self.assertEqual(len({node["y"] for node in tx_nodes}), 1)
        self.assertEqual({node["id"] for node in graph["nodes"]}, {node["id"] for node in result["nodes"]})
        self.assertEqual([(edge["id"], edge["source"], edge["target"]) for edge in graph["edges"]],
                         [(edge["id"], edge["source"], edge["target"]) for edge in result["edges"]])
        for edge in result["edges"]:
            nodes = {node["id"]: node for node in result["nodes"]}
            self.assertEqual(edge["route"][0], attachment_point(nodes[edge["source"]], edge["attachment"]["startItem"]))
            self.assertEqual(edge["route"][-1], attachment_point(nodes[edge["target"]], edge["attachment"]["endItem"]))

    def test_compaction_preserves_rows_and_sibling_direction(self):
        state = change_state(3)
        graph = optimize_graph(build_graph(state), "elbowed", layout_attempts=3)
        result = compact_graph(graph)
        assert_rows(self, result, state)
        self.assertEqual(layout_metrics(result)["node_overlaps"], 0)

    def test_shared_address_is_not_duplicated_or_forced_into_conflicting_row(self):
        state = change_state(1)
        key = next(iter(state["transactions"]))
        state["transactions"][key]["data"]["vout"][0] = copy.deepcopy(state["transactions"][key]["data"]["vout"][1])
        result = optimize_graph(build_graph(state), "elbowed", layout_attempts=3)
        report = result["layout"]["change_outputs"]
        self.assertEqual(report["applied"], [])
        self.assertIn("shared", report["skipped"][0]["reason"])
        self.assertIn("1 skipped", layout_notice(result))
        self.assertIn("Skipped change rows", _preview_html(result, b"<svg></svg>", {}))
        self.assertEqual(sum(node["kind"] == "address" and node["details"]["address"] == "SYNTHETIC-change-0" for node in result["nodes"]), 1)
        unconstrained = build_graph(state)
        # Keep the visible Change caption: its measured width legitimately
        # affects ordinary ELK placement even when a row cannot be enforced.
        unconstrained.pop("change_outputs")
        expected = optimize_graph(unconstrained, "elbowed", layout_attempts=3)
        self.assertEqual([(node["id"], node["x"], node["y"]) for node in result["nodes"]],
                         [(node["id"], node["x"], node["y"]) for node in expected["nodes"]])

    def test_fingerprint_preserves_reuse_but_invalidates_changed_selection(self):
        state = change_state(2)
        graph = build_graph(state)
        result = optimize_graph(graph, layout_attempts=3)
        self.assertEqual(_fingerprint(graph), _fingerprint(result))
        state["service_controls"]["change_outputs"][next(iter(state["transactions"]))]["vout"] = 0
        self.assertNotEqual(_fingerprint(graph), _fingerprint(build_graph(state)))

    def test_reordered_input_has_deterministic_rows(self):
        state = change_state(3)
        original = optimize_graph(build_graph(state), "elbowed", layout_attempts=3)
        state["transactions"] = dict(reversed(list(state["transactions"].items())))
        state["service_controls"]["change_outputs"] = dict(reversed(list(state["service_controls"]["change_outputs"].items())))
        result = optimize_graph(build_graph(state), "elbowed", layout_attempts=3)
        # Peak process memory varies between runs; geometry and ordering do not.
        for graph in (original, result):
            self.assertGreater(graph["layout"]["search"].pop("peak_rss_mb"), 0)
        self.assertEqual(original, result)

    def test_incompatible_change_inputs_to_one_spender_are_reported(self):
        state = change_state(1)
        first = next(iter(state["transactions"]))
        second, joined = txid("second-parent"), txid("joined-change")
        other = copy.deepcopy(state["transactions"][first])
        other["data"]["txid"] = second
        other["data"]["vin"][0]["prevout"] = output("SYNTHETIC-second-external")
        other["data"]["vout"] = [output("SYNTHETIC-second-payment"), output("SYNTHETIC-second-change")]
        state["transactions"][second] = other
        state["transactions"][joined] = {"depth": 1, "observation_id": joined, "data": {
            "txid": joined, "vin": [{"txid": key, "vout": 1, "prevout": state["transactions"][key]["data"]["vout"][1]}
                                       for key in (first, second)], "vout": [output("SYNTHETIC-join")], "status": {}}}
        state["service_controls"]["change_outputs"][second] = {"vout": 1}
        result = optimize_graph(build_graph(state), "elbowed", layout_attempts=3)
        report = result["layout"]["change_outputs"]
        self.assertEqual(report["applied"], [])
        self.assertEqual(len(report["skipped"]), 2)
        self.assertTrue(all("conflicting" in item["reason"] for item in report["skipped"]))
        self.assertEqual(layout_metrics(result)["node_overlaps"], 0)


if __name__ == "__main__":
    unittest.main()
