"""Dense summaries keep every UTXO while simplifying generated Miro captions."""

import copy
import html
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from liquid_tracer.common import TraceError, canonical, read_json
from liquid_tracer.context_groups import group_context_inputs
from liquid_tracer.edge_labels import caption_text
from liquid_tracer.elk_layout import fallback_graph
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, sync, validate_plan
from tests.test_input_order import child_input, input_order_state
from tests.test_presentation_annotations import AnnotationMiro


def dense_graphs(count):
    plain = build_graph(input_order_state(count + 1, continuing=(count,)))
    plain.pop("activity_frames", None)
    plain["run"]["ancestor_runs"] = []
    compact = fallback_graph(group_context_inputs(plain, enabled=True), connector_style="elbowed")
    legacy = copy.deepcopy(compact)
    summary = next(node for node in legacy["nodes"] if node["kind"] == "context_group")
    summary["height"] = max(160, 18 * (count + 1))
    summary["label"] = summary["label"].replace("Input details in local export", "Details in local export")
    for edge in legacy["edges"]:
        edge.pop("caption_display", None)
    return fallback_graph(plain, connector_style="elbowed"), compact, legacy


class DenseMiro(AnnotationMiro):
    """Use real inventory page sizes for summaries with hundreds of inputs."""
    def __call__(self, method, url, headers, body, timeout):
        parsed = urlsplit(url)
        if method == "GET" and parsed.path.endswith("/connectors"):
            self.calls.append((method, url, None))
            start = int(parse_qs(parsed.query).get("cursor", ["0"])[0])
            connectors = [item for item in self.items.values() if item["type"] == "connector"]
            data = connectors[start:start + 50]
            response = {"data": data, "size": len(data), "limit": 50, "total": len(connectors)}
            if start + 50 < len(connectors):
                response["cursor"] = str(start + 50)
            return 200, {}, canonical(response)
        return super().__call__(method, url, headers, body, timeout)


class IncompleteResizeMiro(DenseMiro):
    """Apply one resize but omit geometry from its successful acknowledgment."""
    incomplete_resize_id = None

    def __call__(self, method, url, headers, body, timeout):
        status, response_headers, raw = super().__call__(method, url, headers, body, timeout)
        if (method == "PATCH" and url.rsplit("/", 1)[-1] == self.incomplete_resize_id
                and "geometry" in json.loads(body)):
            self.incomplete_resize_id = None
            response = json.loads(raw)
            response.pop("geometry")
            return status, response_headers, canonical(response)
        return status, response_headers, raw


class DenseContextMiroTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "miro.json"
        self.remote = DenseMiro()

    def sync(self, plan, *, reorganize=True, **options):
        return sync(plan, "synthetic-dense-context-board", self.path, token="test-token", interval=0,
                    transport=self.remote, reorganize=reorganize, **options)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def test_nine_and_251_inputs_hide_captions_without_dropping_connectors_or_evidence(self):
        for count in (9, 251):
            with self.subTest(inputs=count):
                plain, compact, _ = dense_graphs(count)
                plan = make_plan(compact)
                validate_plan(plan)
                summary, = (node for node in compact["nodes"] if node["kind"] == "context_group")
                self.assertEqual((summary["width"], summary["height"]), (240, 160))
                self.assertEqual(len(summary["details"]["members"]), count)
                originals = {edge["id"]: edge for edge in plain["edges"]}
                grouped = {edge["id"]: edge for edge in compact["edges"]}
                connectors = {item["key"]: item for item in plan["connectors"]}
                self.assertEqual(set(connectors), set(originals))
                inputs = {child_input(index) for index in range(count)}
                proof = plan["context_group_items"][summary["id"]]
                self.assertEqual(set(proof["inputs"]), inputs)
                for key, original in originals.items():
                    current, connector = grouped[key], connectors[key]
                    for field in ("id", "target", "label", "quantity", "role", "outpoint", "details"):
                        self.assertEqual(current.get(field), original.get(field))
                    self.assertEqual(caption_text(current, display=False), caption_text(original))
                    self.assertEqual(connector["context_evidence"], {
                        "source": original["source"], "target": original["target"],
                        "outpoint": original.get("outpoint"), "role": original.get("role")})
                    if key in inputs:
                        self.assertEqual(current["original_source"], original["source"])
                        self.assertEqual(connector["source"], summary["id"])
                        self.assertEqual(connector["body"]["captions"], [])
                    else:
                        self.assertEqual(connector["body"]["captions"], [
                            {"content": html.escape(caption_text(original)), "position": "50%"}])
                for endpoint in ("startItem", "endItem"):
                    ports = {tuple(sorted(connectors[key]["attachment"][endpoint]["position"].items()))
                             for key in inputs}
                    self.assertEqual(len(ports), count)

    def test_legacy_251_input_summary_shrinks_in_place_and_keeps_all_ports(self):
        plain, compact, legacy = dense_graphs(251)
        old_plan, plan = make_plan(legacy), make_plan(compact)
        summary, = plan["context_group_items"]
        self.sync(old_plan)
        self.assertEqual(self.item(summary)["geometry"]["height"], 4536)
        before = copy.deepcopy(read_json(self.path)["items"])
        ports = {key: {field: copy.deepcopy(self.item(key)[field]) for field in ("startItem", "endItem")}
                 for key in old_plan["context_group_items"][summary]["inputs"]}
        call_start = len(self.remote.calls)
        report = self.sync(plan, max_items=0)
        mapped = read_json(self.path)["items"]
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["context_items_to_replace"], 0)
        self.assertEqual({key: item["id"] for key, item in mapped.items()},
                         {key: item["id"] for key, item in before.items()})
        self.assertFalse(any(method in ("POST", "DELETE") for method, _, _ in self.remote.calls[call_start:]))
        self.assertEqual(self.item(summary)["geometry"], {"width": 240, "height": 160})
        self.assertEqual(mapped[summary]["context_group_proof"], plan["context_group_items"][summary])
        for key, expected in ports.items():
            self.assertEqual(self.item(key)["captions"], [])
            self.assertEqual({field: self.item(key)[field] for field in expected}, expected)
        call_start = len(self.remote.calls)
        repeated = self.sync(plan, max_items=0)
        self.assertEqual((repeated["created"], repeated["moved"]), (0, 0))
        self.assertFalse(any(method == "PATCH" and "geometry" in body
                             for method, _, body in self.remote.calls[call_start:]))
        # Explicit reorganization reasserts percentage ports because Miro's
        # response cannot prove their fixed mode. Ordinary sync is write-free.
        writes = len(self.remote.writes)
        self.sync(plan, reorganize=False, max_items=0)
        self.assertEqual(len(self.remote.writes), writes)
        # The saved generated-geometry proof must advance with the resize so
        # later ungrouping recognizes this untouched compact summary.
        self.sync(make_plan(plain))
        restored = read_json(self.path)["items"]
        self.assertNotIn(summary, restored)
        self.assertTrue(set(plan["context_group_items"][summary]["members"]).issubset(restored))
        self.assertEqual(set(self.remote.items), {record["id"] for record in restored.values()})

    def test_analyst_caption_survives_dense_caption_cleanup(self):
        _, compact, legacy = dense_graphs(9)
        self.sync(make_plan(legacy))
        key = child_input(0)
        original_id = self.item(key)["id"]
        self.item(key)["captions"] = [{"content": "Analyst evidence note", "position": "37%"}]
        edited = copy.deepcopy(self.item(key)["captions"])
        report = self.sync(make_plan(compact), max_items=0)
        self.assertEqual(self.item(key)["id"], original_id)
        self.assertEqual(self.item(key)["captions"], edited)
        self.assertIn((key, "captions"), {(item["key"], item["field"]) for item in report["conflicts"]})
        self.assertTrue(all(self.item(child_input(index))["captions"] == [] for index in range(1, 9)))

    def test_analyst_summary_resize_is_preserved(self):
        _, compact, legacy = dense_graphs(9)
        plan = make_plan(compact)
        summary, = plan["context_group_items"]
        self.sync(make_plan(legacy))
        self.item(summary)["geometry"].update(width=300, height=420)
        self.sync(plan, max_items=0)
        self.assertEqual(self.item(summary)["geometry"], {"width": 300, "height": 420})

    def test_analyst_summary_rotation_is_preserved(self):
        _, compact, legacy = dense_graphs(9)
        plan = make_plan(compact)
        summary, = plan["context_group_items"]
        self.sync(make_plan(legacy))
        self.item(summary)["geometry"]["rotation"] = 15
        geometry = copy.deepcopy(self.item(summary)["geometry"])
        self.sync(plan, max_items=0)
        self.assertEqual(self.item(summary)["geometry"], geometry)

    def test_uncertain_resize_acknowledgment_recovers_proof_without_repeating_resize(self):
        plain, compact, legacy = dense_graphs(9)
        self.remote = IncompleteResizeMiro()
        plan = make_plan(compact)
        summary, = plan["context_group_items"]
        self.sync(make_plan(legacy))
        summary_id = self.item(summary)["id"]
        self.remote.incomplete_resize_id = summary_id
        with self.assertRaisesRegex(TraceError, "did not acknowledge context-summary dimensions"):
            self.sync(plan, max_items=0)
        uncertain = read_json(self.path)
        self.assertEqual(self.item(summary)["geometry"]["height"], 160)
        self.assertEqual(uncertain["items"][summary]["context_group_proof"]["geometry"]["height"], 180)
        self.assertEqual(uncertain["pending_updates"][summary]["patch"]["geometry"],
                         {"width": 240, "height": 160})
        call_start = len(self.remote.calls)
        self.sync(plan, max_items=0)
        recovered = read_json(self.path)
        self.assertFalse(recovered["pending_updates"])
        self.assertEqual(recovered["items"][summary]["id"], summary_id)
        self.assertEqual(recovered["items"][summary]["context_group_proof"], plan["context_group_items"][summary])
        self.assertFalse(any(method == "PATCH" and url.rsplit("/", 1)[-1] == summary_id and "geometry" in body
                             for method, url, body in self.remote.calls[call_start:]))
        self.assertTrue(all(self.item(child_input(index))["captions"] == [] for index in range(9)))
        self.sync(make_plan(plain))
        self.assertNotIn(summary, read_json(self.path)["items"])

    def test_ordinary_sync_does_not_resize_generated_summary(self):
        _, compact, legacy = dense_graphs(9)
        plan = make_plan(compact)
        summary, = plan["context_group_items"]
        self.sync(make_plan(legacy))
        geometry = copy.deepcopy(self.item(summary)["geometry"])
        self.sync(plan, reorganize=False, max_items=0)
        self.assertEqual(self.item(summary)["geometry"], geometry)

    def test_analyst_summary_note_prevents_automatic_resize(self):
        _, compact, legacy = dense_graphs(9)
        plan = make_plan(compact)
        summary, = plan["context_group_items"]
        self.sync(make_plan(legacy))
        self.item(summary)["data"]["content"] += "<p>Analyst context note</p>"
        content = self.item(summary)["data"]["content"]
        geometry = copy.deepcopy(self.item(summary)["geometry"])
        self.sync(plan, max_items=0)
        self.assertEqual(self.item(summary)["data"]["content"], content)
        self.assertEqual(self.item(summary)["geometry"], geometry)

    def test_ungrouping_restores_original_captions_and_input_evidence(self):
        plain, compact, _ = dense_graphs(9)
        grouped_plan, plan = make_plan(compact), make_plan(plain)
        summary, = grouped_plan["context_group_items"]
        self.sync(grouped_plan)
        self.sync(plan)
        state = read_json(self.path)
        mapped = state["items"]
        saved_connectors = {item["key"]: item for item in state["frame_plan"]["connectors"]}
        self.assertNotIn(summary, mapped)
        self.assertTrue(set(grouped_plan["context_group_items"][summary]["members"]).issubset(mapped))
        self.assertEqual(set(self.remote.items), {record["id"] for record in mapped.values()})
        for connector in plan["connectors"]:
            key = connector["key"]
            self.assertEqual(self.item(key)["captions"], connector["body"]["captions"])
            self.assertEqual(self.item(key)["startItem"]["id"], mapped[connector["source"]]["id"])
            self.assertEqual(self.item(key)["endItem"]["id"], mapped[connector["target"]]["id"])
            self.assertEqual(saved_connectors[key]["context_evidence"], connector["context_evidence"])

    def test_ungrouping_with_analyst_caption_stops_before_deleting_anything(self):
        plain, compact, _ = dense_graphs(9)
        self.sync(make_plan(compact))
        self.item(child_input(0))["captions"] = [{"content": "Analyst evidence note", "position": "50%"}]
        before = copy.deepcopy(self.remote.items)
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "manual edits"):
            self.sync(make_plan(plain))
        self.assertEqual(self.remote.items, before)
        self.assertEqual(len(self.remote.writes), writes)


if __name__ == "__main__":
    unittest.main()
