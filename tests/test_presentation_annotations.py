"""Corner stars and attribution register items survive incremental Miro sync."""
import copy
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.export import build_graph, svg_graph
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.compaction import compact_graph
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import mermaid_source, _preview_html
from liquid_tracer.miro import make_plan, sync, validate_plan
from liquid_tracer.presentation_items import corner
from tests.test_miro_sync import FakeMiro
from tests.test_attribution_convergence import graph_state, annotation, tx


class AnnotationMiro(FakeMiro):
    def __init__(self):
        super().__init__()
        self.lose_delete = False
        self.lose_patch = False
    def __call__(self, method, url, headers, body, timeout):
        if method == "GET" and urlsplit(url).path.endswith("/connectors"):
            self.calls.append((method, url, None))
            data = [value for value in self.items.values() if value["type"] == "connector"]
            return 200, {}, canonical({"data": data, "size": len(data), "limit": 50})
        result = super().__call__(method, url, headers, body, timeout)
        if (method == "DELETE" and self.lose_delete) or (method == "PATCH" and self.lose_patch):
            self.lose_delete = self.lose_patch = False
            raise TraceError("Synthetic lost response")
        return result


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "miro.json"
        self.remote = AnnotationMiro()
        self.state = graph_state((("a:0", "c"), ("b:0", "c"), ("c:0", "d")),
                                labels=[annotation(stop=False)])
        self.graph = build_graph(self.state)

    def plan(self, graph=None):
        plan = make_plan(graph or self.graph)
        validate_plan(plan)
        return plan

    def sync(self, plan, **kwargs):
        return sync(plan, "synthetic-board", self.path, token="test-token", transport=self.remote, interval=0, **kwargs)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def test_annotations_not_in_evidence_graph_or_activity_memberships(self):
        original = copy.deepcopy(self.graph)
        plan = self.plan()
        self.assertEqual(len(plan["presentation_items"]), 2)
        for key in plan["presentation_items"]:
            self.assertFalse(any(key in g["shape_keys"] for g in plan["activity_frames"]["activities"]))
            self.assertFalse(any(e[side] == key for e in plan["connectors"] for side in ("source", "target")))
        self.assertEqual(original, self.graph)

    def test_star_rendered_in_svg_and_mermaid_without_extra_forensic_nodes(self):
        for content in (svg_graph(self.graph), render_svg(self.graph)):
            root = ET.fromstring(content)
            self.assertEqual(len([e for e in root.iter() if e.get("class") == "convergence-badge"]), 1)
        self.assertIn("★", mermaid_source(self.graph))
        self.assertIn("Address attribution register", _preview_html(self.graph, b"<svg/>"))

    def test_initial_sync_idempotence_actual_corner_and_untouched_transaction(self):
        plan = self.plan(); self.sync(plan)
        mapping = copy.deepcopy(read_json(self.path)["items"])
        star = next(k for k,v in plan["presentation_items"].items() if v["kind"] == "convergence")
        host = plan["presentation_items"][star]["host"]
        expected = corner(self.item(host))
        self.assertEqual(tuple(self.item(star)["position"][k] for k in ("x", "y")), expected)
        old_writes = len(self.remote.writes)
        self.sync(plan, max_items=0)
        self.assertEqual(old_writes, len(self.remote.writes))
        self.assertEqual({k:v["id"] for k,v in mapping.items()}, {k:v["id"] for k,v in read_json(self.path)["items"].items()})
        self.item(host)["position"].update(x=7000, y=-7000)
        self.item(host)["geometry"].update(width=300, height=200)
        self.item(host)["rotation"] = 30
        before_host = copy.deepcopy(self.item(host))
        self.sync(plan, max_items=0)
        self.assertEqual(self.item(host), before_host)
        expected = corner(self.item(host))
        self.assertEqual(tuple(self.item(star)["position"][k] for k in ("x", "y")), expected)

    def test_stop_change_updates_indicator_removes_star_and_keeps_trace(self):
        self.sync(self.plan())
        before = copy.deepcopy(self.state)
        changed = copy.deepcopy(self.state); changed["labels"][0]["stop"] = True
        plan = self.plan(build_graph(changed)); self.sync(plan, max_items=0)
        self.assertFalse(any(v.get("presentation_proof", {}).get("kind") == "convergence" for v in read_json(self.path)["items"].values()))
        address = self.item("liquid:address:SYNTHETIC-a-address")
        self.assertIn("STOP TRACING", address["data"]["content"])
        self.assertEqual(self.state, before)

    def test_source_notes_remain_visible_and_paginate_without_raw_html(self):
        state = copy.deepcopy(self.state)
        state["labels"][0]["notes"] = "<script>" + ("Long notes\n" * 300)
        graph = build_graph(state); plan = self.plan(graph)
        notes = [item for item in plan["shapes"] if plan["presentation_items"].get(item["key"],{}).get("kind") == "attribution"]
        self.assertGreater(len(notes), 2)
        self.assertTrue(any('&lt;script&gt;' in s["body"]["data"]["content"] for s in notes))
        self.assertFalse(any('<script>' in s["body"]["data"]["content"] for s in notes))
        self.sync(plan)
        for note in notes:
            self.assertIn("Address attribution" if plan["presentation_items"][note["key"]]["page"] == 0 else "continued", self.item(note["key"])["data"]["content"])

    def test_lost_delete_of_retired_badge_recovers_without_duplicate_creation(self):
        first = self.plan(); self.sync(first)
        state = copy.deepcopy(self.state); state["labels"][0]["stop"] = True
        second = self.plan(build_graph(state))
        self.remote.lose_delete = True
        with self.assertRaisesRegex(TraceError, "lost"):
            self.sync(second)
        self.sync(second, max_items=0)
        self.assertFalse(read_json(self.path).get("pending_deletions"))
        self.assertFalse(any(v.get("presentation_proof",{}).get("kind") == "convergence" for v in read_json(self.path)["items"].values()))

    def test_retiring_register_with_manual_content_blocks_before_writes(self):
        plan = self.plan(); self.sync(plan)
        note = next(k for k,v in plan["presentation_items"].items() if v["kind"] == "attribution")
        self.item(note)["data"]["content"] = "An analyst added evidence"
        state = copy.deepcopy(self.state); state["labels"] = []
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "manual edits"):
            self.sync(self.plan(build_graph(state)))
        self.assertEqual(len(self.remote.writes), writes)

    def test_unmanaged_attachment_to_retiring_badge_blocks_before_writes(self):
        plan = self.plan(); self.sync(plan)
        star = next(k for k,v in plan["presentation_items"].items() if v["kind"] == "convergence")
        self.remote.items["unmanaged-connection"] = {"id":"unmanaged-connection", "type":"connector",
            "startItem":{"id":self.item(star)["id"]}, "endItem":{"id":self.item("tx:" + tx("c"))["id"]}}
        state = copy.deepcopy(self.state); state["labels"][0]["stop"] = True
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "connector attaches"):
            self.sync(self.plan(build_graph(state)))
        self.assertEqual(len(self.remote.writes), writes)

    def test_tampered_proof_badge_position_size_and_connector_rejected(self):
        for action in ("proof", "position", "size", "endpoint"):
            plan = self.plan()
            key = next(k for k,v in plan["presentation_items"].items() if v["kind"] == "convergence")
            shape = next(s for s in plan["shapes"] if s["key"] == key)
            if action == "proof": plan["presentation_items"][key]["host"] = "legend"
            elif action == "position": shape["body"]["position"]["x"] += 50
            elif action == "size": shape["body"]["geometry"]["width"] = 500
            else: plan["connectors"][0]["source"] = key
            plan["sha256"] = digest(canonical({k:v for k,v in plan.items() if k != "sha256"}))
            with self.subTest(action=action), self.assertRaises(TraceError):
                validate_plan(plan)

    def test_real_elk_compaction_and_miro_reorganization_accept_internal_badge(self):
        graph = compact_graph(optimize_graph(self.graph))
        plan = self.plan(graph)
        self.sync(plan, reorganize=True)
        self.sync(plan, reorganize=True, max_items=0)
        self.assertEqual(sum(v.get("presentation_proof",{}).get("kind") == "convergence" for v in read_json(self.path)["items"].values()), 1)

    def test_resized_badge_does_not_bypass_live_overlap_validation(self):
        plan = self.plan(); self.sync(plan)
        star = next(k for k,v in plan["presentation_items"].items() if v["kind"] == "convergence")
        self.item(star)["geometry"]["width"] = 500
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "badge was resized"):
            self.sync(plan)
        self.assertEqual(len(self.remote.writes), writes)

    def test_new_annotation_items_count_against_budget_before_api(self):
        plan = self.plan()
        with self.assertRaisesRegex(TraceError, "max-items"):
            self.sync(plan, max_items=1)
        self.assertEqual(self.remote.calls, [])
