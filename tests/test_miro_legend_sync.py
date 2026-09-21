"""Readable legends update existing boards without moving tracing evidence."""

import copy
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.miro import make_plan, sync
from tests.test_miro_layout import presented
from tests.test_presentation_annotations import AnnotationMiro


def rehash(plan):
    plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
    return plan


def named_graph(count):
    graph = presented()
    names = {f"Service {number}": f"#{number + 1:06x}" for number in range(count)}
    graph["service_controls"] = {"name_colors": {key.casefold(): color for key, color in names.items()}}
    graph["nodes"][0]["details"] = {
        "network": "liquid", "address_attributions": [{"entity": name} for name in names],
        "name_colors": graph["service_controls"]["name_colors"],
    }
    return graph


class LegendSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "miro.json"
        self.remote = AnnotationMiro()

    def sync(self, plan):
        return sync(plan, "board=", self.path, token="synthetic", transport=self.remote, interval=0)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def old_plan(self):
        plan = make_plan(presented())
        legend = plan["shapes"][0]["body"]
        legend["data"]["content"] = "<p>Old dense legend.</p>"
        legend["geometry"] = {"width": 1300, "height": 260}
        legend["style"]["fontSize"] = "14"
        legend["position"]["y"] = -160
        return rehash(plan)

    def test_legacy_legend_grows_upward_with_stable_ids_and_graph_positions(self):
        self.sync(self.old_plan())
        ids = {key: item["id"] for key, item in read_json(self.path)["items"].items()}
        before = {key: copy.deepcopy(self.item(key)) for key in ids}
        # Legacy mappings had no explicit geometry baseline.
        from liquid_tracer.common import save_json
        state = read_json(self.path)
        state["items"]["legend"].pop("legend_geometry", None)
        save_json(self.path, state)
        result = self.sync(make_plan(presented()))
        self.assertEqual(result["created"], 0)
        legend = self.item("legend")
        self.assertIn('style="color:', legend["data"]["content"])
        self.assertIn("●", legend["data"]["content"])
        self.assertGreater(legend["geometry"]["height"], 260)
        old_bottom = before["legend"]["position"]["y"] + before["legend"]["geometry"]["height"] / 2
        self.assertAlmostEqual(legend["position"]["y"] + legend["geometry"]["height"] / 2, old_bottom)
        self.assertEqual(ids, {key: item["id"] for key, item in read_json(self.path)["items"].items()})
        for key in ids.keys() - {"legend"}:
            self.assertEqual(self.item(key), before[key])
        writes = len(self.remote.writes)
        self.assertEqual(self.sync(make_plan(presented()))["updated"], 0)
        self.assertEqual(len(self.remote.writes), writes)

    def test_manual_legend_content_and_geometry_are_preserved(self):
        self.sync(self.old_plan())
        legend = self.item("legend")
        legend["data"]["content"] = "<p>My investigation notes.</p>"
        legend["geometry"]["height"] = 350
        original = copy.deepcopy(legend)
        self.sync(make_plan(presented()))
        self.assertEqual(self.item("legend")["data"], original["data"])
        self.assertEqual(self.item("legend")["geometry"], original["geometry"])
        self.assertEqual(self.item("legend")["position"], original["position"])

    def test_overflow_pages_repeat_and_retire_without_touching_graph(self):
        plan = make_plan(named_graph(45))
        self.assertGreater(len(plan["presentation_items"]), 0)
        self.sync(plan)
        ids = {key: item["id"] for key, item in read_json(self.path)["items"].items()}
        writes = len(self.remote.writes)
        repeat = self.sync(plan)
        self.assertEqual((repeat["created"], repeat["updated"], repeat["deleted"]), (0, 0, 0))
        self.assertEqual(len(self.remote.writes), writes)
        reduced = make_plan(named_graph(1))
        report = self.sync(reduced)
        self.assertEqual(report["deleted"], len(plan["presentation_items"]))
        for item in reduced["shapes"] + reduced["connectors"]:
            self.assertEqual(read_json(self.path)["items"][item["key"]]["id"], ids[item["key"]])

    def test_lost_legend_resize_response_recovers_and_allows_later_growth(self):
        self.sync(self.old_plan())
        transport = self.remote
        failed = [False]

        def lose_response(method, url, headers, body, timeout):
            if failed[0] and method == "GET":
                raise TraceError("Synthetic recovery read unavailable")
            result = transport(method, url, headers, body, timeout)
            if method == "PATCH" and not failed[0]:
                failed[0] = True
                raise TraceError("Synthetic lost update response")
            return result

        with self.assertRaises(TraceError):
            sync(make_plan(presented()), "board=", self.path, token="synthetic", transport=lose_response, interval=0)
        self.assertTrue(read_json(self.path)["pending_updates"])
        self.sync(make_plan(presented()))
        self.assertFalse(read_json(self.path).get("pending_updates"))
        old_height = self.item("legend")["geometry"]["height"]
        self.sync(make_plan(named_graph(3)))
        self.assertGreater(self.item("legend")["geometry"]["height"], old_height)
        self.assertEqual(read_json(self.path)["items"]["legend"]["legend_geometry"],
                         self.item("legend")["geometry"])


if __name__ == "__main__":
    unittest.main()
