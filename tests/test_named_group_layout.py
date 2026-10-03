"""Named centering changes presentation while preserving the complete graph."""

import copy
import json
import unittest

from liquid_tracer.branch_layout import compact_context_inputs
from liquid_tracer.compaction import compact_graph
from liquid_tracer.elk_layout import _request_graph, optimize_graph
from liquid_tracer.elk_parallel import _request
from liquid_tracer.export import build_graph
from liquid_tracer.named_group_layout import (CORE_STRAIGHTNESS, center_metrics,
                                              center_order, group_structure, selected_members)
from tests.test_branch_layout import context_graph
from tests.test_elk_layout import HAS_ELK
from tests.fixtures import output
from tests.test_layout import state_from, txid
from tests.test_service_presentation import designation


def named_fixture():
    graph = {"nodes": [], "edges": [], "fee_items": {}, "graph_options": {"center_name": "Group"},
             "presentation_version": 6}
    def node(key, column, kind="address", selected=False):
        graph["nodes"].append({"id": key, "kind": kind, "column": column, "x": column * 300,
            "y": 100, "width": 80, "height": 80, "label": key,
            "details": {"network": "liquid", "address_attributions": [{"entity": "Group"}] if selected else []}})
    def edge(key, source, target):
        graph["edges"].append({"id": key, "source": source, "target": target})
    for column in (0, 2, 4):
        node("core" + str(column), column, selected=True)
    for column in (1, 3):
        transaction = "tx" + str(column)
        node(transaction, column, "transaction")
        edge("corein" + str(column), "core" + str(column - 1), transaction)
        edge("coreout" + str(column), transaction, "core" + str(column + 1))
        for branch in ("a", "z"):
            before, after = branch + "in" + str(column), branch + "out" + str(column)
            node(before, column - 1)
            node(after, column + 1)
            edge(before, before, transaction)
            edge(after, transaction, after)
    return graph


def named_state():
    transactions = {}
    for index in (1, 2):
        key, parent = txid("named" + str(index)), txid("named" + str(index - 1))
        transactions[key] = {"txid": key,
            "vin": [{"txid": parent, "vout": 0, "prevout": output("SYNTHETIC-core" + str(index))},
                    *({"txid": txid(branch + str(index)), "vout": 0,
                       "prevout": output("SYNTHETIC-" + branch + "in" + str(index))} for branch in ("a", "z"))],
            "vout": [output("SYNTHETIC-core" + str(index + 1)),
                     *(output("SYNTHETIC-" + branch + "out" + str(index)) for branch in ("a", "z"))], "status": {}}
    state = state_from(transactions)
    state["labels"] = [designation("SYNTHETIC-core" + str(index), entity="Group", stop=False)
                       for index in (1, 2, 3)]
    return state


class NamedGroupPreferencesTests(unittest.TestCase):
    def test_members_match_assessment_name_not_display_label_or_other_network(self):
        graph = named_fixture()
        graph["graph_options"]["center_name"] = "  gRoUp  "
        graph["nodes"][0]["details"]["address_attributions"] = [{"name": " GROUP "}]
        graph["nodes"][1]["details"]["network"] = "bitcoin"
        graph["nodes"][2]["label"] = "Some other name"
        graph["nodes"][4]["label"] = "Group"
        original = copy.deepcopy(graph)
        self.assertEqual(selected_members(graph), {"core0", "core4"})
        self.assertEqual(graph, original)

    def test_core_uses_direct_input_and_output_group_members(self):
        graph = named_fixture()
        structure = group_structure(graph)
        self.assertEqual(structure["transactions"], {"tx1", "tx3"})
        self.assertEqual(structure["edges"], {"corein1", "coreout1", "corein3", "coreout3"})
        graph["nodes"][1]["details"]["address_attributions"] = []
        # A path through an unnamed address does not make its transactions
        # direct internal transfers or establish a group-owned value flow.
        self.assertEqual(group_structure(graph)["transactions"], set())

    def test_pegin_and_coinbase_never_create_internal_group_transactions(self):
        for field in ("is_pegin", "is_coinbase"):
            graph = named_fixture()
            graph["edges"][0]["details"] = {"vin": {field: True}}
            structure = group_structure(graph)
            self.assertNotIn("tx1", structure["transactions"])
            self.assertNotIn("corein1", structure["edges"])

    def test_explicit_hubs_keep_their_entry_lane_and_are_reported(self):
        graph = named_fixture()
        graph["nodes"][0]["layout_hub"] = True
        structure = group_structure(graph)
        self.assertNotIn("core0", structure["core"])
        self.assertEqual(structure["excluded_hubs"], {"core0"})
        self.assertEqual(center_metrics(graph)["excluded_hubs"], 1)
        request, _, _ = _request_graph(graph)
        hub = next(node for node in request["children"] if node["id"] == "core0")
        self.assertEqual(hub["layoutOptions"]["elk.partitioning.partition"], "-1")

    def test_blank_and_unmatched_names_leave_worker_request_unchanged(self):
        graph = named_fixture()
        graph["graph_options"] = {}
        ordinary = _request_graph(graph)
        for name in ("", "Unmatched"):
            graph["graph_options"]["center_name"] = name
            self.assertIsNone(center_order(graph))
            self.assertEqual(_request_graph(graph), ordinary)
            self.assertEqual(_request(ordinary[0], 1)["branchProfile"], "flow_weighted")

    def test_centering_is_requested_for_one_attempt_and_contains_no_name(self):
        graph = named_fixture()
        original = copy.deepcopy(graph)
        request, _, _ = _request_graph(graph)
        self.assertEqual(set(request["centerNodeOrder"]), {node["id"] for node in graph["nodes"]})
        self.assertNotIn('"Group"', json.dumps(request))
        self.assertEqual(_request(request, 1)["branchProfile"], "flow_weighted")
        self.assertEqual(_request(request, 2)["branchProfile"], "flow_weighted")
        weights = {edge["id"]: int(edge["layoutOptions"]["elk.layered.priority.straightness"])
                   for edge in request["edges"]}
        self.assertEqual(weights["corein1"], CORE_STRAIGHTNESS)
        self.assertLess(weights["ain1"], CORE_STRAIGHTNESS)
        self.assertEqual(graph, original)

    def test_center_order_keeps_outside_components_together(self):
        graph = named_fixture()
        graph["edges"].append({"id": "outside", "source": "ain1", "target": "aout3"})
        order = center_order(graph)
        self.assertEqual(abs(order.index("ain1") - order.index("aout3")), 1)
        core_positions = [order.index(key) for key in group_structure(graph)["core"]]
        self.assertEqual(max(core_positions) - min(core_positions), len(core_positions) - 1)

    def test_context_compactor_preserves_selected_input_address(self):
        graph = context_graph()
        graph["graph_options"] = {"center_name": "Group"}
        graph["nodes"][0]["details"] = {"address_attributions": [{"entity": "Group"}]}
        before = copy.deepcopy(graph["nodes"])
        compact_context_inputs(graph)
        self.assertEqual(graph["nodes"], before)
        self.assertEqual(graph["layout"]["branch_organization"]["context_inputs_moved"], 0)

    def test_large_disconnected_group_orders_all_objects_deterministically(self):
        graph = named_fixture()
        template = graph["nodes"][0]
        graph["nodes"] = [{**copy.deepcopy(template), "id": "n" + str(index), "column": index % 30}
                          for index in range(1200)]
        for index, node in enumerate(graph["nodes"]):
            if index % 3:
                node["details"]["address_attributions"] = []
        graph["edges"] = []
        order = center_order(graph)
        self.assertEqual(len(order), 1200)
        self.assertEqual(len(set(order)), 1200)
        self.assertEqual(order, center_order(graph))
        core = group_structure(graph)["core"]
        self.assertEqual(len(core), 400)
        positions = [order.index(key) for key in core]
        self.assertEqual(max(positions) - min(positions), 399)


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class NamedGroupEngineTests(unittest.TestCase):
    def assert_preserved(self, before, after):
        self.assertEqual({node["id"] for node in before["nodes"]}, {node["id"] for node in after["nodes"]})
        self.assertEqual([(edge["id"], edge["source"], edge["target"]) for edge in before["edges"]],
                         [(edge["id"], edge["source"], edge["target"]) for edge in after["edges"]])
        self.assertEqual([(node["id"], node["column"], node["details"]) for node in before["nodes"]],
                         [(node["id"], node["column"], node["details"]) for node in after["nodes"]])
        self.assertEqual(after["layout"]["metrics"]["after"]["node_overlaps"], 0)

    def test_one_attempt_aligns_named_spine_and_branches_on_both_sides(self):
        graph = named_fixture()
        original = copy.deepcopy(graph)
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        self.assert_preserved(graph, result)
        self.assertEqual(graph, original)
        core = group_structure(graph)["core"]
        positions = {node["y"] for node in result["nodes"] if node["id"] in core}
        self.assertEqual(len(positions), 1)
        middle = positions.pop()
        outside = [node["y"] for node in result["nodes"] if node["id"] not in core]
        self.assertLess(min(outside), middle)
        self.assertGreater(max(outside), middle)
        self.assertEqual(result["layout"]["named_group"]["alignment_deviation"], 0)
        self.assertEqual(result["layout"]["named_group"]["center_offset"], 0)
        ordinary = copy.deepcopy(graph)
        ordinary["graph_options"]["center_name"] = ""
        ordinary = optimize_graph(ordinary, "elbowed", layout_attempts=1)
        ordinary["graph_options"]["center_name"] = "Group"
        self.assertGreater(center_metrics(ordinary)["alignment_deviation"], 0)

    def test_disconnected_named_addresses_share_a_band_and_survive_compaction(self):
        graph = named_fixture()
        graph["edges"] = [edge for edge in graph["edges"] if not edge["id"].startswith("core")]
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        self.assert_preserved(graph, result)
        self.assertEqual(result["layout"]["named_group"]["alignment_deviation"], 0)
        self.assertEqual(result["layout"]["named_group"]["connecting_transactions"], 0)
        compacted = compact_graph(result)
        selected = selected_members(graph)
        self.assertEqual([(node["id"], node["x"], node["y"]) for node in result["nodes"] if node["id"] in selected],
                         [(node["id"], node["x"], node["y"]) for node in compacted["nodes"] if node["id"] in selected])
        self.assertEqual(compacted["layout"]["named_group"], center_metrics(compacted))

    def test_reused_member_return_connections_keep_single_identity_and_all_routes(self):
        graph = named_fixture()
        graph["edges"].append({"id": "return", "source": "tx3", "target": "core0"})
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        self.assert_preserved(graph, result)
        self.assertEqual(sum(node["id"] == "core0" for node in result["nodes"]), 1)
        returned = next(edge for edge in result["edges"] if edge["id"] == "return")
        self.assertEqual(returned["routing_exception"], "return")
        self.assertTrue(returned["route"])

    def test_same_column_members_stack_without_overlap(self):
        graph = named_fixture()
        extra = copy.deepcopy(graph["nodes"][0])
        extra["id"] = "extra"
        graph["nodes"].append(extra)
        graph["edges"].append({"id": "extra", "source": "extra", "target": "tx1"})
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        self.assert_preserved(graph, result)
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertGreaterEqual(abs(nodes["extra"]["y"] - nodes["core0"]["y"]), 160)

    def test_unmatched_name_has_ordinary_geometry_and_visible_zero_match_report(self):
        graph = named_fixture()
        graph["graph_options"]["center_name"] = ""
        ordinary = optimize_graph(graph, "elbowed", layout_attempts=1)
        graph["graph_options"]["center_name"] = "Unmatched"
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        self.assertEqual(result["nodes"], ordinary["nodes"])
        self.assertEqual(result["edges"], ordinary["edges"])
        self.assertEqual(result["layout"]["named_group"]["matched_addresses"], 0)

    def test_full_size_shapes_captions_and_assessments_produce_aligned_spine(self):
        state = named_state()
        original = copy.deepcopy(state)
        graph = build_graph(state, center_name="Group")
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        self.assert_preserved(graph, result)
        self.assertEqual(state, original)
        self.assertEqual(result["layout"]["named_group"]["alignment_deviation"], 0)
        self.assertEqual(result["layout"]["named_group"]["center_offset"], 0)
        self.assertEqual(result["layout"]["metrics"]["after"]["crossings"], 0)
        self.assertEqual(result["layout"]["metrics"]["after"]["node_intersections"], 0)
        self.assertEqual(result["layout"]["edge_labels"]["reserved_count"], len(graph["edges"]))
        self.assertEqual([(edge["label"], edge["quantity"]) for edge in result["edges"]],
                         [(edge["label"], edge["quantity"]) for edge in graph["edges"]])

    def test_explicit_change_row_keeps_precedence_and_center_report_is_current(self):
        state = named_state()
        first = txid("named1")
        state["service_controls"] = {"change_outputs": {first: {"vout": 1}}}
        graph = build_graph(state, center_name="Group")
        result = optimize_graph(graph, "elbowed", layout_attempts=1)
        self.assert_preserved(graph, result)
        self.assertEqual(len(result["layout"]["change_outputs"]["applied"]), 1)
        nodes = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(nodes["tx:" + first]["y"], nodes["liquid:address:SYNTHETIC-aout1"]["y"])
        self.assertEqual(result["layout"]["named_group"], center_metrics(result))
        self.assertGreater(result["layout"]["named_group"]["alignment_deviation"], 0)


if __name__ == "__main__":
    unittest.main()
