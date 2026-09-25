"""Hub presentation resets at cached-preview and Miro workflow boundaries."""

import copy
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.branch_layout import BRANCH_LAYOUT_VERSION
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.elk_layout import _request_graph, _validate_graph, attachment_point, optimize_graph
from liquid_tracer.export import build_graph
from liquid_tracer.hub_layout import hub_layout_view
from liquid_tracer.input_order import input_orders
from liquid_tracer.layout_preview import export_layout
from liquid_tracer.layout_reuse import _fingerprint, reusable_elk_preview
from liquid_tracer.miro import make_plan, sync, validate_plan
from tests.test_elk_layout import HAS_ELK
from tests.test_elk_miro import PositionMiro
from tests.test_input_order import child_input, input_order_state, west_positions
from tests.test_layout import chain, state_from, txid


def mixed_hub_graph():
    state = input_order_state(3, continuing=(2,))
    state["ancestor_runs"] = []
    graph = build_graph(state)
    edge = next(edge for edge in graph["edges"] if edge["id"] == child_input(2))
    next(node for node in graph["nodes"] if node["id"] == edge["source"])["layout_hub"] = True
    return graph


class HubInputRequestTests(unittest.TestCase):
    def test_internal_views_cannot_be_optimized_or_persisted_as_input_graphs(self):
        graph = mixed_hub_graph()
        for candidate in (hub_layout_view(graph), {**graph, "_hub_layout_view": True},
                          {**graph, "_hub_layout_plan": {"cut_inputs": []}}):
            with self.subTest(keys=set(candidate) - set(graph)):
                with self.assertRaisesRegex(TraceError, "internal layout view"):
                    _validate_graph(candidate, "elbowed")

    def test_hub_reset_does_not_erase_semantic_continuation_priority(self):
        graph = mixed_hub_graph()
        before = copy.deepcopy(graph)
        child = "tx:" + txid("input-order-child")
        expected = [child_input(index) for index in (2, 0, 1)]
        self.assertEqual(input_orders(graph), {child: expected})
        # A same-column presentation intentionally has no forward rank here.
        # That view must not replace the original evidence-based port policy.
        self.assertEqual(input_orders(hub_layout_view(graph)), {})
        request, ports, _ = _request_graph(graph)
        self.assertEqual(request["inputPortOrders"],
                         {child: [ports[key][1] for key in expected]})
        self.assertEqual(graph, before)


@unittest.skipUnless(HAS_ELK, "local ELK installation unavailable")
class HubWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = state_from(chain(5))
        state.update(run_id="1234567890abcdef", ancestor_runs=[])
        cls.original = build_graph(state)
        cls.hub = next(node["id"] for node in cls.original["nodes"] if node["kind"] == "address")
        next(node for node in cls.original["nodes"] if node["id"] == cls.hub)["layout_hub"] = True
        cls.original["graph_options"]["layout_attempts"] = 1
        cls.optimized = optimize_graph(cls.original, "elbowed", layout_attempts=1)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def save_preview(self):
        path = self.root / (self.original["run_id"] + "-elk-12345678")
        export_layout(self.optimized, path)
        return path

    def reuse(self, graph=None):
        return reusable_elk_preview(graph or self.original, self.root, "elbowed", layout_attempts=1)

    def test_completed_hub_preview_preserves_original_columns_and_semantic_fingerprint(self):
        self.save_preview()
        self.assertEqual({node["id"]: node["column"] for node in self.optimized["nodes"]},
                         {node["id"]: node["column"] for node in self.original["nodes"]})
        self.assertEqual(_fingerprint(self.optimized), _fingerprint(self.original))
        self.assertNotIn("_hub_layout_view", self.optimized)
        self.assertNotIn("_hub_layout_plan", self.optimized)
        self.assertEqual(self.reuse(), self.optimized)

    def test_branch_revision_three_preview_is_not_reused(self):
        self.assertGreater(BRANCH_LAYOUT_VERSION, 3)
        path = self.save_preview()
        self.assertEqual(self.reuse(), self.optimized)
        stale = copy.deepcopy(self.optimized)
        stale["layout"]["branch_organization"]["version"] = 3
        report = read_json(path / "layout-report.json")
        report["layout"] = stale["layout"]
        save_json(path / "graph.json", stale)
        save_json(path / "layout-report.json", report)
        self.assertIsNone(self.reuse())

    def test_changing_hub_selection_invalidates_completed_preview(self):
        self.save_preview()
        self.assertEqual(self.reuse(), self.optimized)
        ordinary = copy.deepcopy(self.original)
        next(node for node in ordinary["nodes"] if node["id"] == self.hub).pop("layout_hub")
        self.assertNotEqual(_fingerprint(ordinary), _fingerprint(self.original))
        self.assertIsNone(self.reuse(ordinary))

    def test_miro_reorganize_preserves_single_hub_ids_and_vertical_spender_column(self):
        plan = make_plan(self.optimized)
        validate_plan(plan)
        remote = PositionMiro()
        state_path = self.root / "miro.json"

        def publish(**kwargs):
            return sync(plan, "synthetic-board=", state_path, token="synthetic-token",
                        transport=remote, interval=0, **kwargs)

        publish()
        before = {key: item["id"] for key, item in read_json(state_path)["items"].items()}
        transactions = [node["id"] for node in self.optimized["nodes"] if node["kind"] == "transaction"]
        self.assertEqual(len([node for node in self.optimized["nodes"] if node["id"] == self.hub]), 1)
        self.assertEqual(len({node["x"] for node in self.optimized["nodes"]
                              if node["id"] in transactions}), 1)
        for index, key in enumerate(transactions):
            remote.items[before[key]]["position"]["x"] += (index + 1) * 5000
        manual = {key: copy.deepcopy(remote.items[before[key]]["position"]) for key in transactions}
        ordinary = publish()
        self.assertEqual(ordinary["created"], 0)
        self.assertEqual(manual, {key: remote.items[before[key]]["position"] for key in transactions})
        reorganized = publish(reorganize=True)
        after = {key: item["id"] for key, item in read_json(state_path)["items"].items()}
        self.assertEqual(after, before)
        self.assertEqual(reorganized["created"], 0)
        self.assertGreater(reorganized["moved"], 0)
        positions = {key: remote.items[before[key]]["position"] for key in transactions}
        self.assertEqual(len({position["x"] for position in positions.values()}), 1)
        self.assertLess(remote.items[before[self.hub]]["position"]["x"],
                        min(position["x"] for position in positions.values()))
        for edge in self.optimized["edges"]:
            connector = remote.items[before[edge["id"]]]
            self.assertEqual(connector["startItem"]["id"], before[edge["source"]])
            self.assertEqual(connector["endItem"]["id"], before[edge["target"]])

    def test_mixed_hub_context_policy_survives_final_layout_and_miro_validation(self):
        original = mixed_hub_graph()
        expected = input_orders(original)
        optimized = optimize_graph(original, "elbowed", layout_attempts=1)
        self.assertEqual(input_orders(optimized), expected)
        self.assertEqual(optimized["layout"]["input_order"]["transaction_count"], 1)
        nodes = {node["id"]: node for node in optimized["nodes"]}
        edges = {edge["id"]: edge for edge in optimized["edges"]}
        for order in expected.values():
            if optimized["layout"]["input_order"]["policy"] == "geometry":
                order = sorted(order, key=lambda key: attachment_point(
                    nodes[edges[key]["source"]], edges[key]["attachment"]["startItem"])["y"])
            positions = west_positions(optimized, order)
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(len(positions), len(set(positions)))
        validate_plan(make_plan(optimized))


if __name__ == "__main__":
    unittest.main()
