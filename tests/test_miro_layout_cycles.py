"""Incremental layout may return through real cycles, never acyclic paths."""
import copy
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.miro import _cyclic_connector_keys, _ordinary_placements


def fixture(*, cycle=True, existing_a=0):
    def shape(key, x, y):
        return {"key": key, "body": {"position": {"x": x, "y": y},
                                     "geometry": {"width": 160, "height": 160}}}
    shapes = [shape("a", 1000, 0), shape("b", 0, 400), shape("t", 500, 0)]
    edges = [{"key": "bt", "source": "b", "target": "t"},
             {"key": "ta", "source": "t", "target": "a"}]
    if cycle:
        edges.append({"key": "at", "source": "a", "target": "t"})
    plan = {"shapes": shapes, "connectors": edges, "layout": {"algorithm": "elk_layered_v1"}}
    state = {"items": {key: {"endpoint": "shapes"} for key in ("a", "b")}}
    remote = {item["key"]: copy.deepcopy(item["body"]) for item in shapes[:2]}
    remote["a"]["position"]["x"] = existing_a
    return plan, state, remote


class IncrementalCycleLayoutTests(unittest.TestCase):
    def test_cycle_orientation_can_reverse_without_moving_old_shapes_or_acyclic_parent(self):
        plan, state, remote = fixture()
        before = copy.deepcopy((plan, state, remote))
        positions, shift = _ordinary_placements(plan, state, remote, {}, False)
        self.assertEqual(set(positions), {"t"})
        self.assertEqual(shift, 0)
        self.assertGreaterEqual(positions["t"][0] - 80, remote["b"]["position"]["x"] + 80 + 80)
        self.assertGreater(positions["t"][0], remote["a"]["position"]["x"])
        self.assertEqual((plan, state, remote), before)

    def test_acyclic_conflict_is_still_rejected_even_with_return_metadata(self):
        plan, state, remote = fixture(cycle=False)
        plan["connectors"][1]["routing_exception"] = "return"
        with self.assertRaisesRegex(TraceError, "no left-to-right space"):
            _ordinary_placements(plan, state, remote, {}, False)

    def test_unrelated_cycle_does_not_relax_acyclic_conflicting_edges(self):
        plan, state, remote = fixture(cycle=False)
        for index, key in enumerate(("c", "d")):
            item = copy.deepcopy(plan["shapes"][0])
            item["key"] = key
            item["body"]["position"] = {"x": 3000 + index * 400, "y": 2000}
            plan["shapes"].append(item)
            state["items"][key] = {"endpoint": "shapes"}
            remote[key] = copy.deepcopy(item["body"])
        plan["connectors"] += [{"key": "cd", "source": "c", "target": "d"},
                               {"key": "dc", "source": "d", "target": "c"}]
        with self.assertRaisesRegex(TraceError, "no left-to-right space"):
            _ordinary_placements(plan, state, remote, {}, False)

    def test_satisfied_constraints_keep_existing_placement_without_cycle_scan(self):
        plan, state, remote = fixture(existing_a=2000)
        with patch("liquid_tracer.miro._cyclic_connector_keys", side_effect=AssertionError("No conflict")):
            positions, _ = _ordinary_placements(plan, state, remote, {}, False)
        self.assertEqual(positions["t"][0], 1000)

    def test_deep_cycle_detection_is_iterative_and_deterministic(self):
        count = 3000
        plan = {"shapes": [{"key": str(index)} for index in range(count)],
                "connectors": [{"key": str(index), "source": str(index),
                                "target": str((index + 1) % count)} for index in range(count)]}
        self.assertEqual(_cyclic_connector_keys(plan), {str(index) for index in range(count)})
        plan["shapes"].reverse()
        plan["connectors"].reverse()
        self.assertEqual(_cyclic_connector_keys(plan), {str(index) for index in range(count)})
        # Removing a fee from the search breaks this artificial cycle; a fee
        # edge must not excuse an unrelated forward-placement constraint.
        self.assertEqual(_cyclic_connector_keys(plan, {"0"}), set())


if __name__ == "__main__":
    unittest.main()
