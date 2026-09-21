"""A readable Miro color key remains bounded and safe to sync incrementally."""

import copy
from html.parser import HTMLParser
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer import legend_miro, presentation_items


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def graph():
    return {"simulated": True, "notice": "Synthetic graph for testing.", "nodes": [
        {"id": "fee:1", "kind": "event", "x": 400, "y": -240, "width": 120, "height": 80},
        {"id": "tx:1", "kind": "transaction", "x": 400, "y": 100, "width": 200, "height": 100}],
        "layout": {"annotations": {"legend": {"x": 750, "y": -160}}}}


def rows(count):
    return [{"key": "name:" + str(number), "color": "#123abc", "label": "Service " + str(number),
             "description": "Assigned address color."} for number in range(count)]


class LegendMiroTests(unittest.TestCase):
    def test_normal_key_has_colored_circles_readable_text_and_clears_fee_row(self):
        source = graph()
        before = copy.deepcopy(source)
        shapes, catalog = legend_miro.make_items(source)
        self.assertEqual(source, before)
        self.assertEqual(len(shapes), 1)
        self.assertEqual(catalog, {})
        body = shapes[0]["body"]
        self.assertEqual(shapes[0]["key"], "legend")
        self.assertIn('">●</span> <strong>Selected seed</strong>', body["data"]["content"])
        self.assertEqual(body["style"]["fontSize"], "16")
        self.assertGreater(body["geometry"]["height"], 260)
        self.assertEqual(body["position"]["x"], 750)
        self.assertLessEqual(body["position"]["y"] + body["geometry"]["height"] / 2, -380)
        self.assertEqual(legend_miro.bounds(source), [(
            body["position"]["x"], body["position"]["y"],
            body["geometry"]["width"], body["geometry"]["height"])])

    def test_many_names_use_stable_overflow_pages_without_losing_rows(self):
        with patch.object(legend_miro, "legend_rows", return_value=rows(65)):
            shapes, catalog = legend_miro.make_items(graph())
            again, next_catalog = legend_miro.make_items(graph())
        self.assertEqual((shapes, catalog), (again, next_catalog))
        self.assertGreater(len(shapes), 3)
        self.assertEqual(len(catalog), len(shapes) - 1)
        all_content = "".join(item["body"]["data"]["content"] for item in shapes)
        for row in rows(65):
            self.assertIn("<strong>" + row["label"] + "</strong>", all_content)
        bottoms = set()
        previous_right = None
        for page, item in enumerate(shapes):
            body = item["body"]
            self.assertLessEqual(len(body["data"]["content"]), legend_miro.CONTENT_LIMIT)
            bottom = body["position"]["y"] + body["geometry"]["height"] / 2
            bottoms.add(bottom)
            left = body["position"]["x"] - body["geometry"]["width"] / 2
            if previous_right is not None:
                self.assertGreaterEqual(left - previous_right, legend_miro.PAGE_GAP)
                self.assertEqual(catalog[item["key"]], presentation_items.proof("legend", "legend", page))
            previous_right = left + body["geometry"]["width"]
        self.assertEqual(len(bottoms), 1)

    def test_extreme_legacy_name_escapes_and_splits_without_truncation(self):
        label = "<script>&'\"" * 1400
        row = {"key": "name:long", "color": "#Ab12CD", "label": label, "description": "description"}
        with (patch.object(legend_miro, "legend_rows", return_value=[row]),
              patch.object(legend_miro, "legend_notes", return_value=[])):
            shapes, _ = legend_miro.make_items({"nodes": []})
        self.assertGreater(len(shapes), 1)
        fragments = []
        for item in shapes:
            content = item["body"]["data"]["content"]
            self.assertLessEqual(len(content), legend_miro.CONTENT_LIMIT)
            self.assertNotIn("<script>", content)
            self.assertIn('style="color: #ab12cd"', content)
            # Concatenate only the strong row-label pieces, without titles.
            import re
            for fragment in re.findall(r"<strong>(.*?)</strong>", content):
                if "Liquid UTXO trace" in fragment:
                    continue
                parser = _Text()
                parser.feed(fragment)
                fragments.extend(parser.parts)
        self.assertEqual("".join(fragments), label)

    def test_bad_color_cannot_inject_html(self):
        row = {**rows(1)[0], "color": '#123abc\"><script>'}
        with patch.object(legend_miro, "legend_rows", return_value=[row]), self.assertRaises(TraceError):
            legend_miro.make_items(graph())

    def test_legend_page_proofs_validate_and_cannot_be_connector_endpoints(self):
        with patch.object(legend_miro, "legend_rows", return_value=rows(30)):
            shapes, catalog = legend_miro.make_items(graph())
        plan = {"shapes": shapes, "connectors": [], "presentation_items": catalog}
        self.assertEqual(presentation_items.validate_items(plan), catalog)
        key = next(iter(catalog))
        plan["connectors"] = [{"source": "legend", "target": key}]
        with self.assertRaisesRegex(TraceError, "connector endpoints"):
            presentation_items.validate_items(plan)
        for host, page in (("tx:1", 1), ("legend", 0)):
            with self.assertRaisesRegex(TraceError, "identity"):
                presentation_items.proof("legend", host, page)

    def test_existing_pages_stay_put_and_new_pages_avoid_managed_shapes(self):
        with patch.object(legend_miro, "legend_rows", return_value=rows(45)):
            shapes, catalog = legend_miro.make_items(graph())
        first, second = list(catalog)[:2]
        bodies = {item["key"]: item["body"] for item in shapes}
        remote = {key: copy.deepcopy(bodies[key]) for key in ("legend", first)}
        remote["legend"]["position"].update(x=3000, y=-1000)
        remote[first]["position"].update(x=4380, y=-1000)
        remote["blocker"] = copy.deepcopy(remote[first])
        remote["blocker"]["position"]["x"] = 5760
        state = {"items": {key: {"endpoint": "shapes"} for key in remote}}
        state["items"][first]["presentation_proof"] = catalog[first]
        plan = {"shapes": shapes, "connectors": [], "presentation_items": catalog}
        positions, _ = presentation_items.place_badges(
            plan, state, remote, {}, False, lambda *_: ({}, 0))
        self.assertEqual(positions[first], (4380, -1000))
        self.assertGreater(positions[second][0], 6500)
        self.assertEqual(remote[first]["position"]["x"], 4380)

    def test_reorganize_moves_pages_with_primary_legend(self):
        with patch.object(legend_miro, "legend_rows", return_value=rows(30)):
            shapes, catalog = legend_miro.make_items(graph())
        plan = {"shapes": shapes, "connectors": [], "presentation_items": catalog}
        host = shapes[0]["body"]["position"]
        positions, _ = presentation_items.place_badges(
            plan, {"items": {}}, {}, {}, True,
            lambda *_: ({"legend": (host["x"] + 90, host["y"] - 70)}, 0))
        for item in shapes[1:]:
            position = item["body"]["position"]
            self.assertEqual(positions[item["key"]], (position["x"] + 90, position["y"] - 70))

    def test_reorganize_respects_wide_primary_pages_and_managed_obstacles(self):
        with patch.object(legend_miro, "legend_rows", return_value=rows(45)):
            shapes, catalog = legend_miro.make_items(graph())
        first, second = list(catalog)[:2]
        remote = {item["key"]: copy.deepcopy(item["body"]) for item in shapes}
        remote["legend"]["geometry"]["width"] = 2600
        remote[first]["geometry"]["width"] = 1800
        remote["blocker"] = copy.deepcopy(remote[first])
        remote["blocker"]["position"]["x"] = 3800
        remote["blocker"]["geometry"]["width"] = 300
        state = {"items": {key: {"endpoint": "shapes"} for key in remote}}
        for key, marker in catalog.items():
            state["items"][key]["presentation_proof"] = marker
        host = remote["legend"]["position"]
        plan = {"shapes": shapes, "connectors": [], "presentation_items": catalog}
        positions, _ = presentation_items.place_badges(
            plan, state, remote, {}, True, lambda *_: ({"legend": (host["x"], host["y"])}, 0))
        self.assertGreaterEqual(positions[first][0] - 900, host["x"] + 1300 + legend_miro.PAGE_GAP)
        self.assertGreaterEqual(positions[first][0] - 900, 3800 + 150 + legend_miro.PAGE_GAP)
        self.assertGreaterEqual(positions[second][0] - 650, positions[first][0] + 900 + legend_miro.PAGE_GAP)
        self.assertEqual(remote["legend"]["geometry"]["width"], 2600)
        self.assertEqual(remote[first]["geometry"]["width"], 1800)

    def test_new_transaction_avoids_manually_moved_existing_legend_page(self):
        from liquid_tracer.miro import _bounds, _ordinary_placements, _overlap
        with patch.object(legend_miro, "legend_rows", return_value=rows(30)):
            shapes, catalog = legend_miro.make_items(graph())
        page = next(iter(catalog))
        remote = {item["key"]: copy.deepcopy(item["body"]) for item in shapes}
        remote[page]["position"].update(x=2500, y=500)
        state = {"items": {key: {"endpoint": "shapes"} for key in remote}}
        state["items"][page]["presentation_proof"] = catalog[page]
        transaction = {"key": "tx:new", "body": {
            "data": {"shape": "rectangle", "content": "A new transaction"},
            "position": {"x": 2500, "y": 500}, "geometry": {"width": 180, "height": 100}}}
        plan = {"shapes": shapes + [transaction], "connectors": [], "presentation_items": catalog,
                "layout": {"algorithm": "elk_layered_v1"}}
        positions, _ = presentation_items.place_badges(
            plan, state, remote, {}, False, _ordinary_placements)
        self.assertEqual(positions[page], (2500, 500))
        self.assertFalse(_overlap((*positions["tx:new"], 180, 100), _bounds(remote[page], page)))


if __name__ == "__main__":
    unittest.main()
