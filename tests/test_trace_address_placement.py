"""Bounded pre-routing placement of exact, unpinned address continuations."""

import copy
import unittest

from liquid_tracer.common import TraceError
from liquid_tracer.trace_section_geometry import _ADDRESS_PROBE_LIMIT, _refine_address_rows, assemble
from liquid_tracer.trace_sections import address_neighbors
from tests.test_trace_section_geometry import candidates_for, request_for


def evidence():
    graph = {"nodes": [
        {"id": "tx:parent", "kind": "transaction", "column": 0},
        {"id": "address:a", "kind": "address", "column": 1,
         "details": {"network": "liquid", "address": "SYNTHETIC-address"}},
        {"id": "tx:child", "kind": "transaction", "column": 2}],
        "edges": [
            {"id": "out:parent:1", "source": "tx:parent", "target": "address:a",
             "outpoint": "parent:1", "role": "candidate_output"},
            {"id": "in:child:0", "source": "address:a", "target": "tx:child",
             "outpoint": "parent:1", "role": "traced_input",
             "details": {"vin": {"txid": "parent", "vout": 1}}}],
        "graph_options": {"layout_style": "trace"}}
    request = request_for({node["id"]: node["column"] for node in graph["nodes"]}, [
        ("tx:parent", "address:a", "EAST", "WEST"), ("address:a", "tx:child", "EAST", "WEST")])
    request["nodeShapes"] = {node["id"]: node["kind"] for node in graph["nodes"]}
    return graph, request


def rows_fixture():
    rows = {"parent": 2, "child": 4, "address": 12}
    columns = {"parent": 0, "address": 1, "child": 2}
    for row in range(13):
        rows["other-" + str(row)] = row
        columns["other-" + str(row)] = 3
    return rows, columns, {"address": ["parent", "child"]}


class AddressEligibilityTests(unittest.TestCase):
    def test_exact_continuation_is_selected_without_changing_inputs(self):
        graph, request = evidence()
        original = copy.deepcopy((graph, request))
        self.assertEqual(address_neighbors(graph, request, {}), {"address:a": ["tx:parent", "tx:child"]})
        self.assertEqual((graph, request), original)

    def test_reused_ambiguous_or_nonmatching_evidence_is_excluded(self):
        for change in ("other_outpoint", "other_vin", "missing_vin", "coinbase", "pegin",
                       "duplicate_input", "duplicate_output", "empty_outpoint", "wrong_output_id"):
            with self.subTest(change=change):
                graph, request = evidence()
                before, after = graph["edges"]
                if change == "other_outpoint":
                    after["outpoint"] = "parent:2"
                elif change == "other_vin":
                    after["details"]["vin"]["vout"] = 2
                elif change == "missing_vin":
                    after["details"] = {}
                elif change in ("coinbase", "pegin"):
                    after["details"]["vin"]["is_" + change] = True
                elif change.startswith("duplicate_"):
                    extra = copy.deepcopy(after if change == "duplicate_input" else before)
                    extra["id"] += "-duplicate"
                    graph["edges"].append(extra)
                elif change == "empty_outpoint":
                    before["outpoint"] = after["outpoint"] = ""
                else:
                    before["id"] = "out:another:1"
                self.assertEqual(address_neighbors(graph, request, {}), {})

    def test_backbone_shared_hubs_and_members_are_protected(self):
        for field in ("core", "members", "spine", "excluded_hubs", "shared_hubs"):
            with self.subTest(field=field):
                graph, request = evidence()
                self.assertEqual(address_neighbors(graph, request, {field: ["address:a"]}), {})

    def test_explicit_names_hubs_seed_markers_and_pinned_rows_are_protected(self):
        changes = {
            "hub": lambda graph: graph["nodes"][1].update(layout_hub=True),
            "hub_address": lambda graph: graph["graph_options"].update(hub_addresses=["SYNTHETIC-address"]),
            "name": lambda graph: graph["nodes"][1].update(name="Investigator named address"),
            "seed": lambda graph: graph["nodes"][1].update(role="seed"),
            "seed_marker": lambda graph: graph["nodes"][1].update(is_starting=True),
            "seed_edge": lambda graph: graph["edges"][0].update(role="seed_output"),
            "locked": lambda graph: graph.update(layout={"change_outputs": {"locked_nodes": ["address:a"]}}),
            "alignment": lambda graph: graph.update(layout={"output_alignment": {"outputs": ["address:a"]}}),
            "change_edge": lambda graph: graph["edges"][0].update(change_output={"vout": 1}),
            "change_selection": lambda graph: graph.update(service_controls={"change_outputs": {"parent": {"vout": 1}}}),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                graph, request = evidence()
                change(graph)
                self.assertEqual(address_neighbors(graph, request, {}), {})
        graph, request = evidence()
        graph["graph_options"]["center_name"] = "Known member"
        graph["nodes"][1]["details"]["address_attributions"] = [{"entity": "Known member"}]
        self.assertEqual(address_neighbors(graph, request, {}), {})

    def test_actual_request_columns_must_order_parent_address_child(self):
        for columns in ((0, 0, 2), (0, 2, 2), (0, 3, 2), (2, 1, 0), (0, float("nan"), 2)):
            with self.subTest(columns=columns):
                graph, request = evidence()
                for node, column in zip(request["children"], columns):
                    node["layoutOptions"]["elk.partitioning.partition"] = str(column)
                self.assertEqual(address_neighbors(graph, request, {}), {})


class AddressRowPlacementTests(unittest.TestCase):
    def test_outlier_moves_between_transactions_without_renumbering_other_rows(self):
        rows, columns, hints = rows_fixture()
        original = copy.deepcopy((rows, columns, hints))
        stats = {}
        result = _refine_address_rows(rows, columns, hints, stats=stats)
        self.assertEqual(result["address"], 3)
        self.assertEqual({key: value for key, value in result.items() if key != "address"},
                         {key: value for key, value in rows.items() if key != "address"})
        self.assertLessEqual(set(result.values()), set(rows.values()))
        self.assertEqual((rows, columns, hints), original)
        self.assertEqual(stats["moved"], 1)
        self.assertEqual(stats["row_distance_removed"], 16)
        self.assertLessEqual(stats["probes"], _ADDRESS_PROBE_LIMIT)
        self.assertEqual(result, _refine_address_rows(rows, columns, hints))

    def test_outlier_above_neighbors_and_same_row_neighbors_are_handled(self):
        for parent, child, old in ((8, 10, 0), (4, 4, 12)):
            with self.subTest(rows=(parent, child, old)):
                rows, columns, hints = rows_fixture()
                rows.update(parent=parent, child=child, address=old)
                result = _refine_address_rows(rows, columns, hints)
                self.assertEqual(result["address"], (parent + child) // 2)

    def test_address_already_between_transactions_is_unchanged(self):
        rows, columns, hints = rows_fixture()
        rows["address"] = 3
        stats = {}
        self.assertEqual(_refine_address_rows(rows, columns, hints, stats=stats), rows)
        self.assertEqual((stats["outliers"], stats["moved"], stats["probes"]), (0, 0, 0))

    def test_crowded_column_keeps_original_and_never_adds_a_row(self):
        rows, columns, hints = rows_fixture()
        for row in range(12):
            rows["blocker-" + str(row)] = row
            columns["blocker-" + str(row)] = 1
        stats = {}
        self.assertEqual(_refine_address_rows(rows, columns, hints, stats=stats), rows)
        self.assertEqual(stats["moved"], 0)
        self.assertLessEqual(stats["probes"], _ADDRESS_PROBE_LIMIT)

    def test_competing_addresses_reserve_cells_and_ignore_input_iteration_order(self):
        rows, columns, hints = rows_fixture()
        rows["second"] = 11
        columns["second"] = 1
        hints["second"] = ["parent", "child"]
        stats = {}
        result = _refine_address_rows(rows, columns, hints, stats=stats)
        self.assertEqual(stats["moved"], 2)
        self.assertNotEqual(result["address"], result["second"])
        self.assertEqual(len({(columns[key], row) for key, row in result.items()}), len(rows))
        self.assertEqual(result, _refine_address_rows(dict(reversed(list(rows.items()))),
            dict(reversed(list(columns.items()))), dict(reversed(list(hints.items())))))
        self.assertLessEqual(stats["probes"], len(hints) * _ADDRESS_PROBE_LIMIT)

    def test_probe_count_does_not_grow_with_graph_height(self):
        rows, columns, hints = rows_fixture()
        rows.update(parent=100, child=9000, address=10000)
        for row in range(13, 10001):
            rows["other-" + str(row)] = row
            columns["other-" + str(row)] = 3
        stats = {}
        result = _refine_address_rows(rows, columns, hints, stats=stats)
        self.assertEqual(result["address"], 4550)
        self.assertLessEqual(stats["probes"], _ADDRESS_PROBE_LIMIT)
        self.assertLessEqual(set(result.values()), set(rows.values()))

    def test_assembler_rejects_hints_that_do_not_match_complete_topology(self):
        for change in ("reversed", "not_address", "extra_edge", "backbone", "bad_columns", "unknown"):
            with self.subTest(change=change):
                graph, request = evidence()
                request["addressNeighbors"] = {"address:a": ["tx:parent", "tx:child"]}
                backbone = []
                if change == "reversed":
                    request["addressNeighbors"]["address:a"].reverse()
                elif change == "not_address":
                    request["nodeShapes"]["address:a"] = "transaction"
                elif change == "extra_edge":
                    extra = copy.deepcopy(request["edges"][0])
                    extra["id"] = "extra"
                    request["edges"].append(extra)
                elif change == "backbone":
                    backbone = ["address:a"]
                elif change == "bad_columns":
                    request["children"][1]["layoutOptions"]["elk.partitioning.partition"] = "3"
                else:
                    request["addressNeighbors"] = {"unknown": ["tx:parent", "tx:child"]}
                groups = [[node["id"]] for node in request["children"]]
                with self.assertRaisesRegex(TraceError, "Address-neighbor"):
                    assemble(request, groups, candidates_for(request, groups), backbone)


if __name__ == "__main__":
    unittest.main()
