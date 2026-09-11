"""Scalable fallback placement at the Miro boundary, with no live API calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import read_json
from liquid_tracer.miro import _placements, make_plan, sync, validate_plan
from tests.test_elk_miro import PositionMiro, elk_graph


def fallback_graph(run="one", extended=False, reason="size_limit"):
    value = elk_graph(run, extended)
    value["layout"] = {
        "algorithm": "dependency_layers_v1", "direction": "left_to_right",
        "placement": "complete_graph_v1", "fallback_reason": reason,
    }
    return value


class LargeMiroLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "miro.json"
        self.remote = PositionMiro()

    def sync(self, value=None, **kwargs):
        return sync(make_plan(value or fallback_graph()), "synthetic-board=", self.state_path,
                    token="synthetic-token", transport=self.remote, interval=0, **kwargs)

    def item(self, key):
        return self.remote.items[read_json(self.state_path)["items"][key]["id"]]

    def test_fallback_report_identifies_actual_layout_without_claiming_elk_metrics(self):
        for reason in ("size_limit", "timeout"):
            with self.subTest(reason=reason):
                report = self.sync(fallback_graph(reason=reason), dry_run=True)
                self.assertEqual(report["layout_algorithm"], "dependency_layers_v1")
                self.assertEqual(report["fallback_reason"], reason)
                self.assertNotIn("layout_metrics", report)
                self.assertFalse(self.remote.calls)
                self.assertFalse(self.state_path.exists())

    def test_large_initial_sync_and_reorganization_do_not_repack_each_shape(self):
        value = fallback_graph()
        source = value["nodes"][0]
        value["nodes"] = [{**source, "id": f"addr:{index}", "x": index * 400, "y": 0}
                          for index in range(10001)]
        value["edges"] = []
        plan = make_plan(value)
        validate_plan(plan)
        expected = {item["key"]: (item["body"]["position"]["x"], item["body"]["position"]["y"])
                    for item in plan["shapes"]}
        # An overlap test per earlier shape would turn this into quadratic work.
        # The full layout is already known, and these boards have no obstacles.
        with patch("liquid_tracer.miro._overlap", side_effect=AssertionError("unexpected repacking")):
            positions, shift = _placements(plan, {"items": {}}, {}, {}, False)
            self.assertEqual(positions, expected)
            self.assertEqual(shift, 0)
            state = {"items": {key: {"endpoint": "shapes"} for key in expected}}
            remote = {item["key"]: copy.deepcopy(item["body"]) for item in plan["shapes"]}
            remote["addr:0"]["geometry"]["width"] *= 2
            positions, shift = _placements(plan, state, remote, {}, True)
            self.assertEqual(positions, {key: (x * 2, y * 2) for key, (x, y) in expected.items()})
            self.assertEqual(shift, 0)

    def test_reorganization_retains_complete_coordinates_and_manual_annotations(self):
        value = fallback_graph()
        value["nodes"][1]["width"] = 200
        value["nodes"].append({**value["nodes"][1], "id": "tx:other", "width": 160, "x": 420, "y": 400})
        value["edges"].append({**copy.deepcopy(value["edges"][0]), "id": "input:other:0", "target": "tx:other"})
        self.sync(value)
        self.item("tx:1")["position"].update(x=9000, y=8000)
        self.item("tx:other")["position"].update(x=10000, y=6000)
        self.item("tx:1")["data"]["content"] = "Analyst observation"
        self.item("tx:1")["style"]["fillColor"] = "#123456"
        report = self.sync(value, reorganize=True)
        self.assertGreater(report["moved"], 0)
        self.assertEqual(self.item("tx:1")["position"]["x"], 400)
        self.assertEqual(self.item("tx:other")["position"]["x"], 420)
        self.assertEqual(self.item("tx:other")["position"]["y"], 400)
        self.assertEqual(self.item("tx:1")["data"]["content"], "Analyst observation")
        self.assertEqual(self.item("tx:1")["style"]["fillColor"], "#123456")

    def test_ordinary_continuation_preserves_manual_geometry_ports_and_routing(self):
        self.sync()
        original_mapping = read_json(self.state_path)["items"]
        self.item("addr:a")["position"].update(x=6000, y=800)
        self.item("addr:a")["geometry"].update(width=320, height=200)
        incoming = self.item("input:1:0")
        incoming["shape"] = "curved"
        incoming["endItem"]["position"] = {"x": "50%", "y": "0%"}
        incoming["captions"][0]["content"] = "Analyst observation"
        before = {key: copy.deepcopy(self.item(key))
                  for key in ("addr:a", "tx:1", "addr:b", "input:1:0")}
        report = self.sync(fallback_graph("two", extended=True))
        self.assertEqual((report["created"], report["moved"], report["reattached"]), (5, 0, 0))
        self.assertEqual(before, {key: self.item(key) for key in before})
        current_mapping = read_json(self.state_path)["items"]
        self.assertTrue(all(current_mapping[key]["id"] == record["id"]
                            for key, record in original_mapping.items()))

    def test_legacy_dependency_plan_keeps_its_column_repacking_behavior(self):
        value = fallback_graph()
        del value["layout"]["placement"]
        value["nodes"][1]["width"] = 200
        value["nodes"].append({**value["nodes"][1], "id": "tx:other", "width": 160, "x": 420, "y": 400})
        plan = make_plan(value)
        positions, _ = _placements(plan, {"items": {}}, {}, {}, True)
        self.assertGreater(positions["tx:other"][0], 420)

    def test_real_fallback_ports_and_fee_geometry_survive_plan_and_sync(self):
        from liquid_tracer.elk_layout import fallback_graph as dependency_fallback
        from liquid_tracer.export import build_graph
        from tests.fixtures import fixture
        from tests.test_layout import state_from

        transactions = {data["txid"]: data for key, data in fixture().items()
                        if not key.endswith("outspends")}
        state = state_from(transactions)
        state["ancestor_runs"] = []
        value = dependency_fallback(build_graph(state, include_fees=True))
        plan = make_plan(value)
        validate_plan(plan)
        report = self.sync(value)
        self.assertEqual(report["layout_algorithm"], "dependency_layers_v1")
        self.assertEqual(report["layout_metrics"]["candidate_count"], 0)
        for node in value["nodes"]:
            self.assertEqual(self.item(node["id"])["position"],
                             {"x": node["x"], "y": node["y"], "origin": "center"})
        self.assertEqual(report["created"], len(plan["shapes"]) + len(plan["connectors"]))


if __name__ == "__main__":
    unittest.main()
