"""Branch arrangement changes geometry while retaining verified graph evidence."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.attachment_order import attachment_order_metrics
from liquid_tracer.branch_layout import BRANCH_LAYOUT_VERSION
from liquid_tracer.branch_boundaries import branch_order, boundary_metrics
from liquid_tracer.common import read_json, save_json
from liquid_tracer.compaction import compact_graph
from liquid_tracer.connections import connection_graph
from liquid_tracer.elk_layout import _request_graph, layout_metrics, optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.layout_preview import export_layout
from liquid_tracer.layout_reuse import reusable_elk_preview
from liquid_tracer.miro import make_plan, validate_plan
from tests.fixtures import output
from tests.test_attribution_convergence import graph_state, tx
from tests.test_branch_interactions import receiving
from tests.test_elk_layout import HAS_ELK
from tests.test_elk_parallel import BUDGET, budget_for


SHARED = "SYNTHETIC-boundary-shared-receipt"


def wide_trees(*, joint_spend=False, extras=False):
    """Two traced fan-out trees, linked by receipts or an exact joint spend.

    Every address starts distinct. The sole shared address is deliberately
    attached to an interior sibling on each side, rather than an outer leaf.
    """
    links, seeds, names = [], [], {"a", "b"}
    for root, middle, leaves in (("a", "c", "e"), ("b", "d", "f")):
        for index in range(4):
            child = middle + str(index)
            seeds.append(f"{root}:{index}")
            links.append((f"{root}:{index}", child))
            names.add(child)
            for leaf in range(2):
                name = leaves + str(index * 2 + leaf)
                links.append((f"{child}:{leaf}", name))
                names.add(name)
    if joint_spend:
        links.extend((("e2:0", "99"), ("f4:0", "99")))
        names.add("99")
    state = graph_state(tuple(links), seeds=tuple(seeds))
    state["run_id"] = "0123456789abcdef"
    for name in sorted(names):
        for index in range(len(state["transactions"][tx(name)]["data"]["vout"])):
            receiving(state, f"{name}:{index}", f"SYNTHETIC-{name}-{index}")
    if not joint_spend:
        receiving(state, "e2:0", SHARED)
        receiving(state, "f4:0", SHARED)
    if extras:
        state["transactions"][tx("c1")]["data"]["vin"].append({
            "txid": tx("88"), "vout": 0,
            "prevout": output("SYNTHETIC-untraced-context")})
        fee = output("")
        fee.update(scriptpubkey="", scriptpubkey_type="fee")
        fee.pop("scriptpubkey_address", None)
        state["transactions"][tx("c1")]["data"]["vout"].append(fee)
    return state


def topology(graph):
    return ({node["id"] for node in graph["nodes"]},
            {(edge["id"], edge["source"], edge["target"], edge.get("outpoint"))
             for edge in graph["edges"]})


def collision_gates(graph):
    measured = layout_metrics(graph)
    estimated = layout_metrics(graph, midpoint_elbows=True)
    attachments = attachment_order_metrics(graph)
    return (measured["node_overlaps"], measured["node_intersections"], estimated["node_intersections"],
            measured["crossings"], estimated["crossings"], attachments["endpoint_order_inversions"],
            attachments["coincident_ports"], measured["connector_overlaps"], estimated["connector_overlaps"])


class FilteredBranchTests(unittest.TestCase):
    def test_connection_snapshot_ignores_memberships_for_removed_objects(self):
        state = graph_state((("a:0", "c"), ("c:0", "b"), ("a:1", "d")),
                            seeds=("a:0", "a:1", "b:0", "e:0"))
        original = copy.deepcopy(state)
        graph = connection_graph(state, 2)
        before = copy.deepcopy(graph)
        ids = {node["id"] for node in graph["nodes"]}
        order = branch_order(graph)
        self.assertTrue(order is None or set(order) == ids)
        metrics = boundary_metrics(graph)
        self.assertFalse(metrics["truncated"])
        request, _, _ = _request_graph(graph)
        self.assertTrue(set(request.get("branchNodeOrder", [])) <= ids)
        self.assertEqual(graph, before)
        self.assertEqual(state, original)


@unittest.skipUnless(HAS_ELK, "local ELK package is not installed")
class BoundaryWorkflowTests(unittest.TestCase):
    def test_shared_receipt_transactions_move_to_facing_tree_edges(self):
        state = wide_trees()
        evidence = copy.deepcopy(state)
        graph = build_graph(state)
        before = copy.deepcopy(graph)
        baseline = optimize_graph(graph, "elbowed", layout_attempts=1)
        organized = optimize_graph(graph, "elbowed", layout_attempts=2)
        self.assertLessEqual(collision_gates(organized), collision_gates(baseline))
        old_metrics, new_metrics = boundary_metrics(baseline), boundary_metrics(organized)
        self.assertEqual(old_metrics["interleavings"], new_metrics["interleavings"])
        self.assertGreater(old_metrics["boundary_depth"], 0)
        self.assertEqual(new_metrics["boundary_depth"], 0)
        self.assertLess(new_metrics["interbranch_travel"], old_metrics["interbranch_travel"])
        self.assertTrue(organized["layout"]["branch_organization"]["boundary_ordering"])
        self.assertEqual(topology(organized), topology(graph))
        self.assertEqual(organized["branch_structure"], graph["branch_structure"])
        self.assertEqual(organized["address_convergences"], graph["address_convergences"])
        self.assertFalse(any(node.get("convergence") for node in organized["nodes"]))
        nodes = {node["id"]: node for node in organized["nodes"]}
        root_a, root_b = "tx:" + tx("a"), "tx:" + tx("b")
        memberships = organized["branch_structure"]["node_memberships"]
        own_a = [node["y"] for key, node in nodes.items()
                 if node["column"] == 5 and memberships.get(key) == [root_a]]
        own_b = [node["y"] for key, node in nodes.items()
                 if node["column"] == 5 and memberships.get(key) == [root_b]]
        self.assertEqual(nodes["tx:" + tx("e2")]["y"], max(own_a))
        self.assertEqual(nodes["tx:" + tx("f4")]["y"], min(own_b))
        self.assertLess(max(own_a), min(own_b))
        validate_plan(make_plan(organized))
        compacted = compact_graph(organized)
        self.assertEqual(boundary_metrics(compacted), new_metrics)
        self.assertEqual(topology(compacted), topology(organized))
        self.assertEqual(state, evidence)
        self.assertEqual(graph, before)

    def test_verified_merge_retains_lineage_and_collision_priority(self):
        graph = build_graph(wide_trees(joint_spend=True))
        before = copy.deepcopy(graph)
        baseline = optimize_graph(graph, "elbowed", layout_attempts=1)
        organized = optimize_graph(graph, "elbowed", layout_attempts=2)
        self.assertLessEqual(collision_gates(organized), collision_gates(baseline))
        old_metrics, new_metrics = boundary_metrics(baseline), boundary_metrics(organized)
        self.assertGreater(old_metrics["boundary_depth"], 0)
        self.assertEqual(new_metrics["boundary_depth"], 0)
        self.assertLess(new_metrics["interbranch_travel"], old_metrics["interbranch_travel"])
        self.assertTrue(organized["layout"]["branch_organization"]["boundary_ordering"])
        nodes = {node["id"]: node for node in organized["nodes"]}
        joined = nodes["tx:" + tx("99")]
        self.assertEqual(joined["convergence"]["starting_transaction_indices"], [1, 2])
        self.assertEqual(organized["branch_structure"], graph["branch_structure"])
        self.assertEqual(topology(organized), topology(graph))
        for name in ("e2", "f4"):
            self.assertLess(nodes["tx:" + tx(name)]["x"], joined["x"])
        validate_plan(make_plan(organized))
        self.assertEqual(graph, before)

    def test_context_fees_and_explicit_hubs_keep_their_existing_roles(self):
        graph = build_graph(wide_trees(extras=True), include_fees=True)
        hub = next(node for node in graph["nodes"] if node.get("details", {}).get("address") == SHARED)
        hub["layout_hub"] = True
        before = copy.deepcopy(graph)
        memberships = graph["branch_structure"]["node_memberships"]
        context = next(node for node in graph["nodes"]
                       if node.get("details", {}).get("address") == "SYNTHETIC-untraced-context")
        self.assertNotIn(context["id"], memberships)
        request, _, fee_ids = _request_graph(graph)
        self.assertTrue(fee_ids)
        self.assertFalse(fee_ids & set(request["branchNodeOrder"]))
        organized = optimize_graph(graph, "elbowed", layout_attempts=2)
        found = {node["id"]: node for node in organized["nodes"]}
        self.assertTrue(all(found[hub["id"]]["x"] < node["x"] for node in found.values()
                            if node["kind"] == "transaction"))
        self.assertTrue(all(found[key]["y"] < organized["layout"]["main_top"] for key in fee_ids))
        self.assertEqual(organized["branch_structure"], before["branch_structure"])
        self.assertEqual(organized["fee_items"], before["fee_items"])
        self.assertEqual(topology(organized), topology(before))
        validate_plan(make_plan(organized))
        self.assertEqual(graph, before)

    def test_serial_and_parallel_search_keep_identical_geometry(self):
        graph = build_graph(wide_trees())
        original = copy.deepcopy(graph)
        with patch(BUDGET, side_effect=budget_for(1, 4096)):
            serial = optimize_graph(graph, "elbowed", layout_attempts=4)
        with patch(BUDGET, side_effect=budget_for(2, 4096)):
            parallel = optimize_graph(graph, "elbowed", layout_attempts=4)
        self.assertEqual(serial["layout"]["search"]["execution"], "sequential")
        self.assertEqual(parallel["layout"]["search"]["execution"], "parallel")
        self.assertEqual(serial["nodes"], parallel["nodes"])
        self.assertEqual(serial["edges"], parallel["edges"])
        self.assertEqual(serial["layout"]["metrics"], parallel["layout"]["metrics"])
        self.assertEqual(serial["layout"]["branch_organization"], parallel["layout"]["branch_organization"])
        self.assertEqual(topology(serial), topology(graph))
        self.assertEqual(serial["branch_structure"], graph["branch_structure"])
        self.assertEqual(graph, original)

    def test_old_branch_layout_preview_is_not_reused(self):
        graph = build_graph(wide_trees())
        optimized = optimize_graph(graph, "elbowed", layout_attempts=2)
        self.assertGreater(BRANCH_LAYOUT_VERSION, 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / (graph["run_id"] + "-elk-1234abcd")
            export_layout(optimized, path)
            self.assertEqual(reusable_elk_preview(graph, root, "elbowed", layout_attempts=2), optimized)
            saved = read_json(path / "graph.json")
            saved["layout"]["branch_organization"]["version"] = 1
            report = read_json(path / "layout-report.json")
            report["layout"] = saved["layout"]
            save_json(path / "graph.json", saved)
            save_json(path / "layout-report.json", report)
            self.assertIsNone(reusable_elk_preview(graph, root, "elbowed", layout_attempts=2))


if __name__ == "__main__":
    unittest.main()
