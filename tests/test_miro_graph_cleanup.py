"""Native convergence borders and safe retirement of generated Miro annotations."""
import copy
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.cli import refresh_presentation
from liquid_tracer.export import build_graph, node_csv_rows, svg_graph, COLORS
from liquid_tracer.graph_markers import node_border
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan, sync, validate_plan
from liquid_tracer.compaction import compact_graph
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.attribution_presentation import register_html
from tests.legacy_presentation_items import legacy_plan
from tests.test_attribution_convergence import graph_state, annotation, tx
from tests.test_presentation_annotations import AnnotationMiro


class GraphCleanupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "miro.json"
        self.remote = AnnotationMiro()
        self.state = graph_state((("a:0", "c"), ("b:0", "c"), ("c:0", "d")),
                                 labels=[annotation(stop=False)])
        self.graph = build_graph(self.state)
        self.host = "tx:" + tx("c")

    def sync(self, plan, **kwargs):
        return sync(plan, "synthetic-board", self.path, token="test-token",
                    transport=self.remote, interval=0, **kwargs)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def test_new_plan_has_no_cards_no_stars_and_no_dangling_miro_references(self):
        before = copy.deepcopy(self.graph)
        plan = make_plan(self.graph); validate_plan(plan)
        self.assertEqual(plan["presentation_items"], {})
        self.assertEqual(len(plan["shapes"]), len(self.graph["nodes"]) + 2)
        self.assertFalse(any(s["key"].startswith("annotation:") for s in plan["shapes"]))
        self.assertFalse(any("★" in s["body"]["data"]["content"] for s in plan["shapes"]))
        for node in self.graph["nodes"]:
            shape = next(s for s in plan["shapes"] if s["key"] == node["id"])
            if node.get("attribution_reference"):
                self.assertNotIn(node["attribution_reference"], shape["body"]["data"]["content"])
        self.assertEqual(self.graph, before)
        self.assertIn("Review A", register_html(self.graph))
        row = next(row for row in node_csv_rows(self.graph) if row.get("notes"))
        self.assertEqual(row["notes"], "Review A\nReview B")
        self.assertEqual(row["source"], "Supplied records <not HTML>")

    def test_marker_is_same_native_border_in_every_renderer(self):
        plan = make_plan(self.graph)
        for shape in plan["shapes"]:
            if shape["key"] == self.host:
                self.assertEqual(shape["body"]["style"]["borderColor"], "#ff0000")
                self.assertEqual(shape["body"]["style"]["borderWidth"], "12")
                self.assertEqual(shape["body"]["style"]["fillColor"], COLORS["transaction"])
        for svg, attribute in ((svg_graph(self.graph), "data-key"), (render_svg(self.graph), "data-node-id")):
            root = ET.fromstring(svg)
            group = next(el for el in root.iter() if el.get(attribute) == self.host)
            box = next(el for el in group if el.tag.endswith("rect"))
            self.assertEqual((box.get("stroke"), box.get("stroke-width")), ("#ff0000", "12"))
            self.assertFalse(any(el.get("class") == "convergence-badge" for el in root.iter()))
        source = mermaid_source(self.graph)
        self.assertIn("stroke:#ff0000,stroke-width:12px", source)
        self.assertNotIn("★", source)
        self.assertEqual(node_border({"kind": "address", "convergence": {"irrelevant": True}}), ("#334155", 2))

    def test_eligible_start_keeps_purple_box_and_selected_seed_red(self):
        graph = build_graph(graph_state((("a:0", "b"), ("b:0", "c"))))
        starting = next(n for n in graph["nodes"] if n["id"] == "tx:" + tx("b"))
        self.assertEqual(node_border(starting), ("#ff0000", 12))
        self.assertEqual(starting["color"], COLORS["starting_transaction"])
        selected = [n for n in graph["nodes"] if n.get("role") == "seed"]
        self.assertTrue(selected)
        self.assertTrue(all(n["color"] == COLORS["seed"] for n in selected))

    def test_shared_address_is_separate_from_input_merge_and_single_lineage_is_not_marked(self):
        for state in (graph_state(raw_links=(("a:0", "c"), ("b:0", "c"))),
                      graph_state((("a:0", "c"), ("a:1", "c")), seeds=("a:0", "a:1"))):
            for record in state["transactions"].values():
                for out in record["data"]["vout"]:
                    out["scriptpubkey_address"] = "SYNTHETIC-shared"
            graph = build_graph(state)
            self.assertFalse(any(n.get("convergence") for n in graph["nodes"]))
            if len({seed.rpartition(":")[0] for seed in state["seeds"]}) == 1:
                self.assertTrue(all(node_border(n) == ("#334155", 2) for n in graph["nodes"]))
            else:
                self.assertTrue(graph["address_convergences"])
                context_tx = next(n for n in graph["nodes"] if n["id"] == "tx:" + tx("c"))
                self.assertEqual(node_border(context_tx), ("#334155", 2))

    def test_upgrade_retires_old_cards_and_stars_without_recreating_graph(self):
        old = legacy_plan(self.graph); validate_plan(old); self.sync(old)
        old_catalog = old["presentation_items"]
        self.assertEqual(len(old_catalog), 2)
        mapping = read_json(self.path)["items"]
        retained = {k: copy.deepcopy(v) for k,v in mapping.items() if k not in old_catalog}
        self.item(self.host)["position"].update(x=9000, y=-8000)
        self.item(self.host)["geometry"].update(width=320, height=240)
        self.item(self.host)["rotation"] = 30
        before = copy.deepcopy(self.item(self.host))
        report = self.sync(make_plan(self.graph), max_items=0)
        self.assertEqual(report["deleted"], 2)
        self.assertEqual(report["created"], 0)
        after = read_json(self.path)["items"]
        self.assertEqual({k:v["id"] for k,v in retained.items()}, {k:v["id"] for k,v in after.items()})
        self.assertTrue(all(k not in after for k in old_catalog))
        for field in ("position", "geometry", "rotation"):
            self.assertEqual(self.item(self.host)[field], before[field])
        self.assertEqual(self.item(self.host)["style"]["borderWidth"], "12")
        self.assertEqual(self.item(self.host)["style"]["borderColor"], "#ff0000")
        writes = len(self.remote.writes)
        self.sync(make_plan(self.graph), max_items=0)
        self.assertEqual(writes, len(self.remote.writes))

    def test_refresh_of_saved_legacy_plan_keeps_evidence_and_retires_annotations(self):
        old = legacy_plan(self.graph); self.sync(old)
        trace_path = self.path.parent / "trace.json"
        save_json(trace_path, self.state)
        before = trace_path.read_bytes()
        with patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must not retrace")):
            plan = refresh_presentation(old, trace_path)
        self.assertEqual(plan["presentation_version"], 15)
        self.assertEqual(plan["presentation_items"], {})
        self.sync(plan, max_items=0)
        self.assertEqual(self.item(self.host)["style"]["borderWidth"], "12")
        self.assertEqual(trace_path.read_bytes(), before)

    def test_changed_detection_restores_normal_border_without_retracing(self):
        self.sync(make_plan(self.graph))
        changed = copy.deepcopy(self.state); changed["labels"][0]["stop"] = True
        self.sync(make_plan(build_graph(changed)), max_items=0)
        self.assertEqual(self.item(self.host)["style"]["borderColor"], "#334155")
        self.assertEqual(self.item(self.host)["style"]["borderWidth"], "2")

    def test_manual_border_edits_remain_protected_and_reported(self):
        self.sync(legacy_plan(self.graph))
        self.item(self.host)["style"]["borderColor"] = "#123456"
        report = self.sync(make_plan(self.graph), max_items=0)
        self.assertEqual(self.item(self.host)["style"]["borderColor"], "#123456")
        self.assertTrue(report["conflicts"])

    def test_lost_deletion_response_resumes_same_cleanup(self):
        self.sync(legacy_plan(self.graph))
        plan = make_plan(self.graph); self.remote.lose_delete = True
        with self.assertRaisesRegex(TraceError, "lost"):
            self.sync(plan, max_items=0)
        self.sync(plan, max_items=0)
        mapping = read_json(self.path)
        self.assertFalse(mapping.get("pending_deletions"))
        self.assertFalse(any(k.startswith("annotation:") for k in mapping["items"]))
        self.assertEqual(self.item(self.host)["style"]["borderWidth"], "12")

    def test_manual_notes_block_removal_before_any_writes(self):
        old = legacy_plan(self.graph); self.sync(old)
        note = next(k for k,v in old["presentation_items"].items() if v["kind"] == "attribution")
        self.item(note)["data"]["content"] += "<p>Investigator annotation</p>"
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "manual edits"):
            self.sync(make_plan(self.graph))
        self.assertEqual(writes, len(self.remote.writes))

    def test_connector_attached_to_old_card_blocks_cleanup(self):
        old = legacy_plan(self.graph); self.sync(old)
        note = next(k for k,v in old["presentation_items"].items() if v["kind"] == "attribution")
        self.remote.items["manual"] = {"id":"manual", "type":"connector",
            "startItem":{"id":self.item(note)["id"]}, "endItem":{"id":self.item(self.host)["id"]}}
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "connector attaches"):
            self.sync(make_plan(self.graph))
        self.assertEqual(writes, len(self.remote.writes))

    def test_real_elk_compaction_uses_border_without_extra_objects(self):
        original = copy.deepcopy(self.graph)
        compacted = compact_graph(optimize_graph(self.graph))
        plan = make_plan(compacted); validate_plan(plan)
        self.sync(plan, reorganize=True)
        self.assertEqual(self.item(self.host)["style"]["borderWidth"], "12")
        self.assertEqual(plan["presentation_items"], {})
        self.assertEqual(self.graph, original)

    def test_long_notes_do_not_inflate_miro_shape_count_or_board_metrics(self):
        first = make_plan(self.graph)
        changed = copy.deepcopy(self.state); changed["labels"][0]["notes"] = "Long notes " * 350
        graph = build_graph(changed); second = make_plan(graph)
        self.assertEqual(len(first["shapes"]), len(second["shapes"]))
        self.assertEqual(first["frames"], second["frames"])
        self.assertEqual(compact_graph(optimize_graph(self.graph))["layout"]["compaction"]["after"]["board"],
                         compact_graph(optimize_graph(graph))["layout"]["compaction"]["after"]["board"])
        self.assertIn(changed["labels"][0]["notes"], register_html(graph))


if __name__ == "__main__":
    unittest.main()
