import copy
import math
import unittest

from liquid_tracer.common import TraceError
from liquid_tracer.miro_frames import (
    FRAME_PADDING, FRAME_TITLE_SPACE, activity_frames, frame_bodies, validate_activity_frames,
)


def node(key, starting=False, address=None):
    return {"id": key, "kind": "transaction" if key.startswith("tx:") else "address",
            "role": "starting_transaction" if starting else "address",
            "details": {"address": address}}


def edge(key, source, target):
    return {"id": key, "source": source, "target": target}


def three_trees():
    return {"nodes": [node("tx:" + name, True) for name in "abc"]
            + [node("output:" + name) for name in "abc"],
            "edges": [edge("edge:" + name, "tx:" + name, "output:" + name) for name in "abc"]}


def rectangle(frame):
    body = frame["body"]
    x, y = body["position"]["x"], body["position"]["y"]
    width, height = body["geometry"]["width"], body["geometry"]["height"]
    return (x - width / 2, y - height / 2, x + width / 2, y + height / 2)


class ActivityFrameGroupingTests(unittest.TestCase):
    def test_three_trees_then_two_joining_trees(self):
        graph = three_trees()
        first = activity_frames(graph)
        bounds = {item["id"]: (index * 400, 0, 100, 100) for index, item in enumerate(graph["nodes"])}
        self.assertEqual(len(frame_bodies(first, bounds)), 4)
        graph["edges"].append(edge("merge", "output:a", "tx:b"))
        continued = activity_frames(graph)
        self.assertEqual(len(frame_bodies(continued, bounds)), 3)
        merged, separate = continued["activities"]
        self.assertEqual(merged["starting_transaction_keys"], ["tx:a", "tx:b"])
        self.assertEqual(merged["shape_keys"], ["output:a", "output:b", "tx:a", "tx:b"])
        self.assertEqual(merged["connector_keys"], ["edge:a", "edge:b", "merge"])
        self.assertEqual(merged["key"], first["activities"][0]["key"])
        self.assertEqual(separate["key"], first["activities"][2]["key"])
        self.assertEqual(merged["title"], "Activity 1 · 2 starting transactions")
        self.assertEqual(separate["title"], "Activity 2 · 1 starting transaction")

    def test_growth_and_new_later_seed_keep_starting_anchor(self):
        graph = three_trees()
        before = activity_frames(graph)["activities"][0]["key"]
        graph["nodes"] += [node("before-all-other-keys"), node("tx:z", True)]
        graph["edges"] += [edge("growth", "output:a", "before-all-other-keys"),
                           edge("extra-start", "before-all-other-keys", "tx:z")]
        after = activity_frames(graph)["activities"][0]
        self.assertEqual(before, after["key"])
        self.assertEqual(after["starting_transaction_keys"], ["tx:a", "tx:z"])

    def test_cycles_and_backwards_edges_use_weak_connectivity(self):
        graph = {"nodes": [node("tx:a", True), node("out:a"), node("out:b")],
                 "edges": [edge("one", "tx:a", "out:a"), edge("two", "out:b", "out:a"),
                           edge("three", "out:b", "tx:a")]}
        groups = activity_frames(graph)["activities"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["shape_keys"], ["out:a", "out:b", "tx:a"])

    def test_repeated_address_strings_do_not_join_distinct_occurrences(self):
        graph = three_trees()
        for item in graph["nodes"]:
            if item["kind"] == "address":
                item["details"]["address"] = "same synthetic address"
        self.assertEqual(len(activity_frames(graph)["activities"]), 3)

    def test_component_and_membership_order_is_independent_of_input(self):
        graph = three_trees()
        expected = activity_frames(graph)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(activity_frames(graph), expected)

    def test_isolated_node_without_seed_still_has_an_activity(self):
        graph = {"nodes": [node("out:alone")], "edges": []}
        metadata = activity_frames(graph)
        self.assertEqual(metadata["activities"][0]["starting_transaction_keys"], [])
        self.assertEqual(metadata["activities"][0]["shape_keys"], ["out:alone"])
        self.assertEqual(len(frame_bodies(metadata, {"out:alone": (0, 0, 100, 100)})), 2)

    def test_seed_outpoints_identify_older_graphs_without_roles(self):
        graph = {"run": {"seeds": ["a:0", "a:1"]},
                 "nodes": [node("tx:a")], "edges": []}
        self.assertEqual(activity_frames(graph)["activities"][0]["starting_transaction_keys"], ["tx:a"])

    def test_reserved_keys_duplicate_ids_and_missing_endpoints_are_rejected(self):
        for reserved in ("legend", "run:one", "frame:graph", "frame:activity:a"):
            with self.subTest(reserved=reserved), self.assertRaises(TraceError):
                activity_frames({"nodes": [node(reserved)], "edges": []})
        invalid = [
            {"nodes": [node("a"), node("a")], "edges": []},
            {"nodes": [node("a")], "edges": [edge("one", "a", "missing")]},
            {"nodes": [node("a")], "edges": [edge("a", "a", "a")]},
            {"nodes": [node("a")], "edges": [edge("one", "a", "a"), edge("one", "a", "a")]},
        ]
        for graph in invalid:
            with self.subTest(graph=graph), self.assertRaises(TraceError):
                activity_frames(graph)

    def test_hundred_thousand_node_chain_is_iterative(self):
        count = 100_001
        graph = {"nodes": [{"id": f"node:{index}", "kind": "address"} for index in range(count)],
                 "edges": [edge(f"edge:{index}", f"node:{index - 1}", f"node:{index}")
                           for index in range(1, count)]}
        metadata = activity_frames(graph)
        self.assertEqual(len(metadata["activities"]), 1)
        self.assertEqual(len(metadata["activities"][0]["shape_keys"]), count)
        self.assertEqual(len(metadata["activities"][0]["connector_keys"]), count - 1)


class ActivityFrameValidationTests(unittest.TestCase):
    def setUp(self):
        self.graph = three_trees()
        self.metadata = activity_frames(self.graph)
        self.shape_keys = [item["id"] for item in self.graph["nodes"]] + ["legend", "run:one", "run:two"]
        self.connectors = [{"key": item["id"], "source": item["source"], "target": item["target"]}
                           for item in self.graph["edges"]]

    def validate(self, value=None, **kwargs):
        validate_activity_frames(self.metadata if value is None else value,
                                 self.shape_keys, self.connectors, **kwargs)

    def test_valid_partition_excludes_notes(self):
        self.validate()
        self.validate(starting_transaction_keys=["tx:a", "tx:b", "tx:c"])

    def test_missing_duplicated_foreign_and_note_members_are_rejected(self):
        for member in (None, "tx:b", "foreign-shape", "legend", "run:one"):
            value = copy.deepcopy(self.metadata)
            if member is None:
                value["activities"][0]["shape_keys"].pop()
            else:
                value["activities"][0]["shape_keys"].append(member)
            with self.subTest(member=member), self.assertRaises(TraceError):
                self.validate(value)

    def test_wrong_component_edges_or_frame_identity_are_rejected(self):
        for field, replacement in (("connector_keys", ["edge:b"]), ("key", "frame:activity:foreign"),
                                   ("starting_transaction_keys", ["output:a"]), ("title", "Foreign title")):
            value = copy.deepcopy(self.metadata)
            value["activities"][0][field] = replacement
            with self.subTest(field=field), self.assertRaises(TraceError):
                self.validate(value)
        self.connectors.append({"key": "merge", "source": "tx:a", "target": "tx:b"})
        with self.assertRaises(TraceError):
            self.validate()

    def test_saved_run_seed_keys_are_authoritative(self):
        with self.assertRaises(TraceError):
            self.validate(starting_transaction_keys=["tx:a", "tx:b"])

    def test_boolean_schema_is_rejected(self):
        value = copy.deepcopy(self.metadata)
        value["schema_version"] = True
        with self.assertRaises(TraceError):
            self.validate(value)


class ActivityFrameBoundsTests(unittest.TestCase):
    def setUp(self):
        self.metadata = activity_frames({"nodes": [node("tx:a", True)], "edges": []})

    def test_activity_title_padding_and_outer_nesting(self):
        frames = frame_bodies(self.metadata, {"tx:a": (100, 200, 20, 40)})
        outer, activity = frames
        self.assertEqual(rectangle(activity), (30, 30, 170, 280))
        self.assertEqual(rectangle(outer), (-30, -120, 230, 340))
        self.assertEqual(activity["body"]["data"]["type"], "freeform")
        self.assertEqual(activity["body"]["data"]["format"], "custom")
        self.assertEqual(activity["body"]["style"]["fillColor"], "#ffffffff")
        for item in frames:
            self.assertNotIn("parent", item["body"])
            self.assertNotIn("relativeTo", item["body"]["position"])

    def test_complete_frame_includes_all_saved_run_notes(self):
        bounds = {"tx:a": (100, 200, 20, 40), "run:old": (1000, -500, 400, 100)}
        outer, activity = frame_bodies(self.metadata, bounds)
        self.assertEqual(rectangle(activity), (30, 30, 170, 280))
        self.assertEqual(rectangle(outer), (-30, -700, 1260, 340))
        moved = frame_bodies(self.metadata, {**bounds, "tx:a": (-2000, 3000, 600, 400)})
        self.assertEqual(rectangle(moved[1]), (-2360, 2650, -1640, 3260))

    def test_outer_covers_disjoint_activity_frames(self):
        graph = three_trees()
        metadata = activity_frames(graph)
        bounds = {item["id"]: (index * 500, index * -900, 150, 250)
                  for index, item in enumerate(graph["nodes"])}
        frames = frame_bodies(metadata, bounds)
        outer = rectangle(frames[0])
        for item in frames[1:]:
            left, top, right, bottom = rectangle(item)
            self.assertLessEqual(outer[0], left - FRAME_PADDING)
            self.assertLessEqual(outer[1], top - FRAME_PADDING - FRAME_TITLE_SPACE)
            self.assertGreaterEqual(outer[2], right + FRAME_PADDING)
            self.assertGreaterEqual(outer[3], bottom + FRAME_PADDING)

    def test_missing_member_and_invalid_bounds_fail(self):
        with self.assertRaises(TraceError):
            frame_bodies(self.metadata, {})
        for bounds in ((0, 0, 0, 100), (0, 0, 100, -1), (math.inf, 0, 1, 1),
                       (0, math.nan, 1, 1), (True, 0, 1, 1), (1, 2, 3)):
            with self.subTest(bounds=bounds), self.assertRaises(TraceError):
                frame_bodies(self.metadata, {"tx:a": bounds})

    def test_large_finite_coordinates_are_allowed(self):
        frames = frame_bodies(self.metadata, {"tx:a": (1e12, -1e12, 1e9, 1e9)})
        self.assertEqual(len(frames), 2)
        self.assertGreater(frames[0]["body"]["geometry"]["width"], 1e9)

    def test_empty_graph_retains_complete_frame(self):
        metadata = activity_frames({"nodes": [], "edges": []})
        self.assertEqual(len(frame_bodies(metadata, {})), 1)


if __name__ == "__main__":
    unittest.main()
