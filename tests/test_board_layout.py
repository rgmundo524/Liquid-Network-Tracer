"""Board-aware updates use live board geometry and preserve manual organization."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from liquid_tracer import board_layout
from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.miro import make_plan, sync, validate_plan
from tests.test_miro_sync import FakeMiro, graph
from tests.test_board_projection_safety import scope_plan as projected


class BoardMiro(FakeMiro):
    def __call__(self, method, url, headers, body, timeout):
        if method == "GET" and urlsplit(url).path.endswith("/connectors"):
            self.calls.append((method, url, None))
            return 200, {}, canonical({"data": [item for item in self.items.values() if item.get("type") == "connector"]})
        if method == "GET" and urlsplit(url).path.endswith("/items"):
            self.calls.append((method, url, None))
            return 200, {}, canonical({"data": [item for item in self.items.values() if item.get("type") != "connector"]})
        return super().__call__(method, url, headers, body, timeout)


class BoardLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.json"
        self.remote = BoardMiro()
        self.first = projected(make_plan(graph()))
        self.sync(self.first)

    def sync(self, plan):
        return sync(plan, "test-board", self.path, token="test", transport=self.remote, interval=0)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def prepare(self, source=None):
        source = source or graph("two", True)
        snapshot = board_layout.capture("test-board", self.path, self.first["namespace"],
                                         token="test", transport=self.remote, interval=0)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda value, **kw: value) as elk:
            result = board_layout.prepare_graph(source, snapshot)
        plan = projected(make_plan(result))
        validate_plan(plan)
        return plan, elk

    def test_only_new_nodes_use_elk_and_occupied_unmanaged_area_is_avoided(self):
        self.item("addr:b")["position"].update(x=1800, y=-900)
        self.remote.items["analyst-image"] = {"id": "analyst-image", "type": "image",
            "position": {"x": 20000, "y": 10}, "geometry": {"width": 1000, "height": 800},
            "data": {"title": "private analyst material"}}
        plan, elk = self.prepare()
        self.assertEqual({node["id"] for node in elk.call_args.args[0]["nodes"]}, {"tx:2", "addr:c"})
        self.assertEqual([edge["id"] for edge in elk.call_args.args[0]["edges"]], ["output:2:0"])
        before = copy.deepcopy(self.remote.items)
        report = self.sync(plan)
        self.assertEqual(report["created"], 4)
        self.assertEqual(self.item("addr:b")["position"], before[self.item("addr:b")["id"]]["position"])
        self.assertGreater(self.item("tx:2")["position"]["x"] - 80, 20500)
        self.assertEqual(self.item("input:2:0")["startItem"]["id"], self.item("addr:b")["id"])
        self.assertNotIn("private analyst material", canonical(plan["board_layout"]).decode())
        writes = len(self.remote.writes)
        self.sync(plan)
        self.assertEqual(writes, len(self.remote.writes))

    def test_move_after_plot_prevents_all_writes(self):
        plan, _ = self.prepare()
        self.item("addr:a")["position"]["x"] += 20
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "board changed"):
            self.sync(plan)
        self.assertEqual(writes, len(self.remote.writes))

    def test_unmanaged_item_added_after_plot_prevents_all_writes(self):
        plan, _ = self.prepare()
        self.remote.items["note"] = {"id": "note", "type": "text", "position": {"x": 0, "y": 0},
                                     "geometry": {"width": 200, "height": 100}}
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "board changed"):
            self.sync(plan)
        self.assertEqual(writes, len(self.remote.writes))

    def test_parent_relative_unmanaged_item_contributes_canvas_bounds(self):
        self.remote.items.update({"frame": {"id": "frame", "type": "frame", "position": {"x": 10000, "y": 100},
            "geometry": {"width": 1000, "height": 1000}}, "child": {"id": "child", "type": "text",
            "parent": {"id": "frame"}, "position": {"x": 2000, "y": 500, "relativeTo": "parent_top_left"},
            "geometry": {"width": 200, "height": 200}}})
        plan, _ = self.prepare()
        self.assertEqual(plan["board_layout"]["geometry"]["child"], [11500., 100., 200., 200.])
        self.sync(plan)
        self.assertGreater(self.item("tx:2")["position"]["x"] - 80, 11600)

    def test_moved_obsolete_generated_node_can_be_removed(self):
        self.item("addr:b")["position"]["y"] = 1700
        source = graph("two")
        source["nodes"] = [node for node in source["nodes"] if node["id"] != "addr:b"]
        source["edges"] = [edge for edge in source["edges"] if edge["target"] != "addr:b"]
        plan, elk = self.prepare(source)
        elk.assert_not_called()
        result = self.sync(plan)
        self.assertEqual(result["deleted"], 2)

    def test_manual_note_on_retiring_node_is_protected(self):
        self.item("addr:b")["data"]["content"] = "Investigator note"
        source = graph("two")
        source["nodes"] = [node for node in source["nodes"] if node["id"] != "addr:b"]
        source["edges"] = [edge for edge in source["edges"] if edge["target"] != "addr:b"]
        plan, _ = self.prepare(source)
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "manual text/style"):
            self.sync(plan)
        self.assertEqual(writes, len(self.remote.writes))

    def test_acknowledged_creations_resume_after_connector_rejection(self):
        plan, _ = self.prepare()
        original = self.remote
        rejected = False
        def transport(method, url, headers, body, timeout):
            nonlocal rejected
            if method == "POST" and urlsplit(url).path.endswith("/connectors") and not rejected:
                rejected = True
                return 400, {}, b'{}'
            return original(method, url, headers, body, timeout)
        with self.assertRaisesRegex(TraceError, "HTTP 400"):
            sync(plan, "test-board", self.path, token="test", transport=transport, interval=0, workers=1)
        created_ids = {key: record["id"] for key, record in read_json(self.path)["items"].items()}
        self.assertIn("tx:2", created_ids)
        self.sync(plan)
        after = read_json(self.path)["items"]
        self.assertEqual(after["tx:2"]["id"], created_ids["tx:2"])
        self.assertEqual(len(self.remote.items), len(after))

    def test_lost_patch_ack_resumes_with_expected_applied_fields(self):
        source = graph("two", True)
        source["nodes"][0]["color"] = "#AA00FF"
        source["nodes"][0]["label"] = "Updated\nAttribution"
        plan, _ = self.prepare(source)
        original = self.remote
        failed = False
        def transport(method, url, headers, body, timeout):
            nonlocal failed
            if method == "PATCH" and not failed:
                original(method, url, headers, body, timeout)
                item = original.items[url.rsplit("/", 1)[-1]]
                item["style"]["fillColor"] = item["style"]["fillColor"].lower()
                item["style"]["fontSize"] = float(item["style"]["fontSize"])
                item["data"]["content"] = item["data"]["content"].replace("<br>", "<br />")
                failed = True
                raise TraceError("Lost PATCH response")
            if failed and method == "GET":
                return 503, {}, b'{}'
            return original(method, url, headers, body, timeout)
        with self.assertRaisesRegex(TraceError, "could not be verified"):
            sync(plan, "test-board", self.path, token="test", transport=transport, interval=0, workers=1)
        self.sync(plan)
        self.assertEqual(self.item("addr:a")["style"]["fillColor"].lower(), "#aa00ff")
        self.assertIn("Attribution", self.item("addr:a")["data"]["content"])

    def test_mapping_changes_are_rejected_before_writes(self):
        from liquid_tracer.common import save_json
        plan, _ = self.prepare()
        state = read_json(self.path)
        first = state["items"]["addr:a"]["id"]
        state["items"]["addr:a"]["id"] = state["items"]["addr:b"]["id"]
        state["items"]["addr:b"]["id"] = first
        save_json(self.path, state)
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "mapping changed"):
            self.sync(plan)
        self.assertEqual(writes, len(self.remote.writes))

    def test_unmanaged_summary_with_missing_geometry_uses_detail_read(self):
        self.remote.items["image"] = {"id": "image", "type": "image", "position": {"x": 15000, "y": 200},
            "geometry": {"width": 900, "height": 800}}
        original = self.remote
        def transport(method, url, headers, body, timeout):
            status, response_headers, raw = original(method, url, headers, body, timeout)
            if method == "GET" and urlsplit(url).path.endswith("/items"):
                import json
                page = json.loads(raw)
                for item in page["data"]:
                    if item["id"] == "image":
                        item["geometry"].pop("height")
                raw = canonical(page)
            return status, response_headers, raw
        snapshot = board_layout.capture("test-board", self.path, self.first["namespace"],
                                          token="test", transport=transport, interval=0)
        self.assertEqual(snapshot["geometry"]["image"], [15000., 200., 900., 800.])
        self.assertTrue(any(urlsplit(url).path.endswith("/items/image") for method, url, _ in original.calls))

    def test_real_elk_addition_retains_translated_routes_and_labels(self):
        import shutil
        if not shutil.which("node") or not Path("layout/node_modules/elkjs").is_dir():
            self.skipTest("Local ELK runtime is unavailable")
        source = graph("two", True)
        for index, node in enumerate(source["nodes"]):
            node["column"] = index
        snapshot = board_layout.capture("test-board", self.path, self.first["namespace"],
                                          token="test", transport=self.remote, interval=0)
        result = board_layout.prepare_graph(source, snapshot, layout_attempts=1)
        internal = next(edge for edge in result["edges"] if edge["id"] == "output:2:0")
        self.assertTrue(internal["route"])
        self.assertIn("label_layout", internal)
        self.assertTrue(all(point["x"] > snapshot["bounds"][2] for point in internal["route"]))
        from liquid_tracer.layout_preview import render_svg
        self.assertIn(b"Miro board update layout", render_svg(result))
        self.sync(projected(make_plan(result)))

    def test_reviewed_resize_rotation_and_frame_parent_do_not_block_retirement(self):
        self.remote.items["frame"] = {"id": "frame", "type": "frame", "position": {"x": 1000, "y": 1000},
                                      "geometry": {"width": 2000, "height": 2000}}
        node = self.item("addr:b")
        node["position"].update(x=800, y=400, relativeTo="parent_top_left")
        node["geometry"].update(width=220, height=280, rotation=20)
        node["parent"] = {"id": "frame"}
        source = graph("two")
        source["nodes"] = [node for node in source["nodes"] if node["id"] != "addr:b"]
        source["edges"] = [edge for edge in source["edges"] if edge["target"] != "addr:b"]
        plan, _ = self.prepare(source)
        self.assertEqual(self.sync(plan)["deleted"], 2)
        self.assertIn("frame", self.remote.items)
